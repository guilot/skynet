"""Motor de reglas de una posición, dirigido por eventos.

Única definición de las reglas de salida de la estrategia. Lo alimentan
tanto el backtest (con velas históricas) como el bot en vivo (con velas
sintéticas por tick), de modo que ambos ejecutan literalmente el mismo
código de decisión.

Protocolo de dos fases: el motor NO ejecuta salidas, las propone
(`ExitIntent`). El driver las ejecuta -el backtest al precio de referencia,
el bot contra el broker- y devuelve el `Fill` real con `on_fill`. Las reglas
que dependen del precio obtenido (la subida del stop a break-even) se
deciden ahí, con el precio realmente ejecutado y no con el teórico.

Una "vela" es solo una ventana de observación de precio: el backtest pasa
velas de un minuto con las transiciones de ese minuto; el bot puede pasar
una vela sintética (open=high=low=close=último precio) en cada tick, con las
transiciones recién llegadas, y obtiene la misma semántica de orden.
"""
from __future__ import annotations

from collections.abc import Sequence

from scanner_volumen.models import Direction, State
from scanner_volumen.strategy.model import (
    CandleRow, ExitIntent, ExitReason, Fill, MIN_MS, StrategyParams, TransitionRow,
)

_HOT = State.HOT.rank
_SIGNAL = State.SIGNAL.rank
_EXTREME = State.EXTREME.rank


class PositionRules:
    def __init__(self, entry: TransitionRow, params: StrategyParams) -> None:
        self.entry = entry
        self.params = params
        self._es_long = entry.direction is Direction.LONG
        signo = 1.0 if self._es_long else -1.0
        self._stop_price = entry.price * (1 - params.stop_pct * signo)
        self._max_rank = entry.new_state.rank
        # `restante` solo se mueve al confirmar un fill; `comprometido`
        # descuenta además lo ya emitido y pendiente. Un mismo evento puede
        # emitir varias intenciones (WATCH -> EXTREME dispara HOT, SIGNAL y el
        # cierre a la vez), y el cierre "del resto" debe valer 0.34, no 1.0.
        self._restante = 1.0
        self._comprometido = 1.0
        self._pendientes: list[ExitIntent] = []

    # --- estado observable ---

    @property
    def cerrada(self) -> bool:
        return self._restante <= 0

    @property
    def restante(self) -> float:
        return self._restante

    @property
    def max_rank(self) -> int:
        return self._max_rank

    # --- eventos ---

    def on_candle(
        self, vela: CandleRow, transiciones: Sequence[TransitionRow] = ()
    ) -> list[ExitIntent]:
        if self._pendientes:
            raise ValueError(
                "hay intenciones sin confirmar: el driver debe llamar a on_fill "
                "por cada ExitIntent antes del siguiente evento"
            )
        if self.cerrada:
            return []

        intents: list[ExitIntent] = []

        # (1) stop dentro de la vela. Si salta, no se evalúa nada más.
        toca_stop = (
            (vela.low <= self._stop_price) if self._es_long
            else (vela.high >= self._stop_price)
        )
        if toca_stop:
            paso_al_abrir = (
                (vela.open <= self._stop_price) if self._es_long
                else (vela.open >= self._stop_price)
            )
            precio = vela.open if paso_al_abrir else self._stop_price
            self._emitir(intents, vela.ts, self._comprometido, ExitReason.STOP, precio)
            return intents

        return intents

    def on_fill(self, fill: Fill) -> None:
        indice = next(
            (i for i, it in enumerate(self._pendientes)
             if it.reason is fill.reason and it.ts == fill.ts),
            None,
        )
        if indice is None:
            raise ValueError(
                f"el fill {fill.reason.value}@{fill.ts} no corresponde a ninguna "
                "intención pendiente"
            )
        del self._pendientes[indice]
        self._restante -= fill.fraction

    # --- interno ---

    def _emitir(
        self, intents: list[ExitIntent], ts: int, fraction: float,
        reason: ExitReason, precio: float,
    ) -> None:
        intent = ExitIntent(ts=ts, fraction=fraction, reason=reason,
                            precio_referencia=precio)
        intents.append(intent)
        self._pendientes.append(intent)
        self._comprometido -= fraction
