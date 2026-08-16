# scanner_volumen/scoring/states.py
"""Máquina de estados por símbolo.

Subir de estado es inmediato; bajar exige caer por debajo del umbral menos un
margen durante varios ticks seguidos. Sin esa histéresis, un símbolo oscilando
alrededor de 80 entra y sale de SIGNAL varias veces por minuto y el dashboard
resulta ilegible.
"""
from __future__ import annotations

from dataclasses import dataclass

from scanner_volumen.config import StatesConfig
from scanner_volumen.models import State

ESTADO_MINIMO_DE_ALERTA = State.SIGNAL


@dataclass
class _SymbolState:
    state: State = State.NORMAL
    below_ticks: int = 0
    last_alert_ms: int | None = None
    # severidad (estado) de la última alerta emitida; es lo que decide si un
    # escalado posterior debe saltarse el cooldown, no el estado
    # inmediatamente anterior a la transición actual (que puede haber pasado
    # por un estado más bajo entre medias sin que eso reste gravedad).
    last_alert_state: State | None = None


@dataclass(frozen=True)
class Transition:
    symbol: str
    previous: State
    current: State
    score: float
    escalated: bool
    should_alert: bool
    ts: int


class StateMachine:
    """Histéresis por símbolo con alertas de subida y cooldown."""

    def __init__(self, cfg: StatesConfig) -> None:
        self._cfg = cfg
        self._states: dict[str, _SymbolState] = {}

    def state_of(self, symbol: str) -> State:
        return self._states.get(symbol, _SymbolState()).state

    def _estado_para(self, score: float) -> State:
        if score >= self._cfg.extreme:
            return State.EXTREME
        if score >= self._cfg.signal:
            return State.SIGNAL
        if score >= self._cfg.hot:
            return State.HOT
        if score >= self._cfg.watch:
            return State.WATCH
        return State.NORMAL

    def _umbral_de(self, estado: State) -> float:
        return {
            State.EXTREME: self._cfg.extreme,
            State.SIGNAL: self._cfg.signal,
            State.HOT: self._cfg.hot,
            State.WATCH: self._cfg.watch,
            State.NORMAL: 0.0,
        }[estado]

    def update(self, symbol: str, score: float, now_ms: int) -> Transition | None:
        actual = self._states.setdefault(symbol, _SymbolState())
        candidato = self._estado_para(score)
        anterior = actual.state

        if candidato.rank > anterior.rank:
            actual.below_ticks = 0
            nuevo = candidato
        elif candidato.rank < anterior.rank:
            # solo baja si además cae por debajo del margen, y de forma sostenida
            if score < self._umbral_de(anterior) - self._cfg.exit_margin:
                actual.below_ticks += 1
                if actual.below_ticks < self._cfg.exit_ticks:
                    return None
                actual.below_ticks = 0
                nuevo = candidato
            else:
                # se recupera dentro del margen: el contador de salida se
                # reinicia por completo, no solo se congela
                actual.below_ticks = 0
                return None
        else:
            actual.below_ticks = 0
            return None

        actual.state = nuevo
        escalado = nuevo.rank > anterior.rank

        alerta = False
        if escalado and nuevo.rank >= ESTADO_MINIMO_DE_ALERTA.rank:
            en_cooldown = False
            peor_que_la_ultima_alerta = True
            if actual.last_alert_ms is not None:
                en_cooldown = (
                    now_ms - actual.last_alert_ms < self._cfg.cooldown_minutes * 60_000
                )
                peor_que_la_ultima_alerta = nuevo.rank > actual.last_alert_state.rank

            if not en_cooldown or peor_que_la_ultima_alerta:
                alerta = True
                actual.last_alert_ms = now_ms
                actual.last_alert_state = nuevo

        return Transition(
            symbol=symbol,
            previous=anterior,
            current=nuevo,
            score=score,
            escalated=escalado,
            should_alert=alerta,
            ts=now_ms,
        )
