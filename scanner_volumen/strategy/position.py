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
        entry_rank = entry.new_state.rank
        self._fired_hot = entry_rank >= _HOT      # no se cobra el tramo del nivel de entrada
        self._fired_signal = entry_rank >= _SIGNAL
        self._stop_en_be = False
        # `restante` solo se mueve al confirmar un fill; `comprometido`
        # descuenta además lo ya emitido y pendiente. Un mismo evento puede
        # emitir varias intenciones (WATCH -> EXTREME dispara HOT, SIGNAL y el
        # cierre a la vez), y el cierre "del resto" debe valer 0.34, no 1.0.
        self._restante = 1.0
        self._comprometido = 1.0
        self._pendientes: list[ExitIntent] = []
        self._corriendo_extreme = False   # tras EXTREME, mantener el resto
        self._extreme_hold_until = 0
        self._extreme_run_ms = int(params.extreme_run_min * MIN_MS)
        self._ultimo_cambio_ts = entry.ts   # timer de estancamiento
        self._be_armado = False
        self._stale_ms = params.stale_min * MIN_MS

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

    @property
    def stop_price(self) -> float:
        """Precio de stop vigente. La Fase 3 lo usa para reflejar en el
        exchange una orden stop reduce-only que sobreviva a una caída del
        bot, sin depender de recalcularlo por su cuenta."""
        return self._stop_price

    @property
    def stop_en_be(self) -> bool:
        """True si el stop ya subió a break-even. Le indica al driver que
        debe mover la orden stop colocada en el exchange en consecuencia."""
        return self._stop_en_be

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

        # run tras EXTREME: mantener el resto hasta extreme_run_min y cerrar a
        # mercado. Las transiciones se ignoran durante el run; solo el stop
        # (arriba, ya en BE por las parciales) puede sacar antes.
        if self._corriendo_extreme:
            if vela.ts >= self._extreme_hold_until:
                self._emitir(intents, vela.ts, self._comprometido,
                             ExitReason.EXTREME, vela.close)
            return intents

        # (2) transiciones cuyo ts cae en esta ventana, en orden ascendente
        for t in transiciones:
            # cualquier transición es un cambio de estado: reinicia el timer y
            # desarma una salida en BE pendiente
            self._ultimo_cambio_ts = t.ts
            self._be_armado = False
            if t.new_state.rank > self._max_rank:
                self._max_rank = t.new_state.rank
            if not self._fired_hot and self._max_rank >= _HOT:
                self._fired_hot = True
                self._emitir(intents, t.ts, self.params.tramo_hot,
                             ExitReason.SCALE_HOT, t.price)
            if not self._fired_signal and self._max_rank >= _SIGNAL:
                self._fired_signal = True
                self._emitir(intents, t.ts, self.params.tramo_signal,
                             ExitReason.SCALE_SIGNAL, t.price)
            if self._max_rank >= _EXTREME:
                if self._extreme_run_ms <= 0:
                    self._emitir(intents, t.ts, self._comprometido,
                                 ExitReason.EXTREME, t.price)
                else:
                    # deja correr el resto: cierra por timer (o por stop en BE)
                    self._corriendo_extreme = True
                    self._extreme_hold_until = t.ts + self._extreme_run_ms
                break

        if self._comprometido <= 0 or self._corriendo_extreme:
            return intents

        # (3) estancamiento: pasados stale_min sin cambio de estado se arma una
        # salida limitada en break-even. Cierra en cuanto el precio toca la
        # entrada (o a mercado si ya está en profit); nunca peor que BE.
        if not self._be_armado and vela.ts - self._ultimo_cambio_ts >= self._stale_ms:
            self._be_armado = True
        if self._be_armado:
            alcanza_be = (
                (vela.high >= self.entry.price) if self._es_long
                else (vela.low <= self.entry.price)
            )
            if alcanza_be:
                if self._es_long:
                    precio = vela.open if vela.open >= self.entry.price else self.entry.price
                else:
                    precio = vela.open if vela.open <= self.entry.price else self.entry.price
                self._emitir(intents, vela.ts, self._comprometido,
                             ExitReason.STALE_BE, precio)

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
        # tras cualquier parcial en beneficio, el stop del resto sube a
        # break-even y se queda ahí. Se juzga con el precio EJECUTADO: con
        # slippage, una parcial teóricamente ganadora puede salir en pérdida.
        if (fill.reason in (ExitReason.SCALE_HOT, ExitReason.SCALE_SIGNAL)
                and not self._stop_en_be):
            en_beneficio = (
                (fill.price > self.entry.price) if self._es_long
                else (fill.price < self.entry.price)
            )
            if en_beneficio:
                self._stop_price = self.entry.price
                self._stop_en_be = True

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
