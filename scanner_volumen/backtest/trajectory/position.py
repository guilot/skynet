"""Simulador de una única posición de trayectoria.

Función pura sobre las transiciones y velas de UN símbolo, independiente del
margen (los `fills` son fracciones del tamaño original). El orden dentro de
cada vela es: (1) stop, (2) transiciones a su ts, (3) estancamiento;
el stop primero es la elección conservadora del spec.

Tras cualquier salida parcial en beneficio (SCALE_HOT/SCALE_SIGNAL cuyo precio
sea favorable frente a la entrada), el stop del resto de la posición sube a
break-even (el precio de entrada) y se mantiene ahí.

Si pasan `stale_min` minutos sin ningún cambio de estado (cualquier transición
reinicia el contador), se arma una salida limitada en break-even: el resto se
cierra en cuanto el precio vuelve a la entrada -o a mercado si ya está en
profit-, nunca por debajo de BE; mientras siga bajo agua, solo lo sostiene el
stop de -stop_pct.

Al llegar a EXTREME: si `extreme_run_min` es 0 se cierra el resto en el acto
(al precio del cruce); si es >0 se mantiene el resto ese tiempo y se cierra a
mercado, con el stop (ya en BE por las parciales) todavía activo por si revierte.
"""
from __future__ import annotations

from scanner_volumen.models import Direction, State
from scanner_volumen.strategy.model import (
    CandleRow, ExitReason, Fill, MIN_MS, PositionOutcome, StrategyParams, TransitionRow,
)

_HOT = State.HOT.rank
_SIGNAL = State.SIGNAL.rank
_EXTREME = State.EXTREME.rank


def simulate_position(
    entry: TransitionRow,
    later: list[TransitionRow],
    candles: list[CandleRow],
    params: StrategyParams,
) -> PositionOutcome:
    if not candles:
        # No debería alcanzarse en producción: run_trajectory filtra antes las
        # entradas sin cobertura de velas (ver Finding 1 / retención). Este
        # guard convierte cualquier violación futura del contrato en un error
        # explícito en vez de un IndexError silencioso más abajo.
        raise ValueError("simulate_position requiere al menos una vela")

    es_long = entry.direction is Direction.LONG
    signo = 1.0 if es_long else -1.0
    stop_price = entry.price * (1 - params.stop_pct * signo)

    entry_rank = entry.new_state.rank
    max_rank = entry_rank
    fired_hot = entry_rank >= _HOT      # no se dispara un tramo del nivel de entrada
    fired_signal = entry_rank >= _SIGNAL
    stop_en_be = False
    ultimo_cambio_ts = entry.ts         # para el timer de estancamiento
    be_armado = False
    corriendo_extreme = False           # tras EXTREME, mantener el resto extreme_run_min
    extreme_hold_until = 0

    fills: list[Fill] = []
    restante = 1.0
    trans_por_ts = _agrupar_por_ventana(later, candles)
    stale_ms = params.stale_min * MIN_MS
    extreme_run_ms = int(params.extreme_run_min * MIN_MS)

    def cerrar(ts: float, price: float, reason: ExitReason) -> None:
        nonlocal restante
        if restante <= 0:
            return
        fills.append(Fill(ts=int(ts), price=price, fraction=restante, reason=reason))
        restante = 0.0

    for c in candles:
        if restante <= 0:
            break
        # (1) stop dentro de la vela (sigue activo incluso durante el run tras EXTREME)
        toca_stop = (c.low <= stop_price) if es_long else (c.high >= stop_price)
        if toca_stop:
            paso_al_abrir = (c.open <= stop_price) if es_long else (c.open >= stop_price)
            precio = c.open if paso_al_abrir else stop_price
            cerrar(c.ts, precio, ExitReason.STOP)
            break

        # run tras EXTREME: mantener el resto hasta extreme_run_min y cerrar a
        # mercado. Las transiciones y el estancamiento se ignoran durante el run;
        # solo el stop (arriba, en BE tras las parciales) puede sacarnos antes.
        if corriendo_extreme:
            if c.ts >= extreme_hold_until:
                cerrar(c.ts, c.close, ExitReason.EXTREME)
                break
            continue

        # (2) transiciones cuyo ts cae en [c.ts, c.ts + 1min)
        for t in trans_por_ts.get(c.ts, ()):  # orden ascendente garantizado
            # cualquier transición es un cambio de estado: reinicia el timer de
            # estancamiento y desarma una salida en BE pendiente.
            ultimo_cambio_ts = t.ts
            be_armado = False
            if t.new_state.rank > max_rank:
                max_rank = t.new_state.rank
            # tramos por niveles estrictamente por encima del rank de entrada
            hubo_parcial = False
            if not fired_hot and max_rank >= _HOT:
                fired_hot = True
                hubo_parcial = True
                restante -= params.tramo_hot
                fills.append(Fill(ts=t.ts, price=t.price, fraction=params.tramo_hot,
                                  reason=ExitReason.SCALE_HOT))
            if not fired_signal and max_rank >= _SIGNAL:
                fired_signal = True
                hubo_parcial = True
                restante -= params.tramo_signal
                fills.append(Fill(ts=t.ts, price=t.price, fraction=params.tramo_signal,
                                  reason=ExitReason.SCALE_SIGNAL))
            # tras cualquier salida parcial en beneficio, el stop del resto sube
            # a break-even (precio de entrada) y se queda ahí. Aplica desde la
            # vela siguiente, porque el stop se evalúa al inicio de cada vela.
            if hubo_parcial and not stop_en_be:
                en_beneficio = (t.price > entry.price) if es_long else (t.price < entry.price)
                if en_beneficio:
                    stop_price = entry.price
                    stop_en_be = True
            if max_rank >= _EXTREME:
                if extreme_run_ms <= 0:
                    cerrar(t.ts, t.price, ExitReason.EXTREME)
                else:
                    # deja correr el resto: cierre por timer (o por stop en BE)
                    corriendo_extreme = True
                    extreme_hold_until = t.ts + extreme_run_ms
                break
        if restante <= 0:
            break
        if corriendo_extreme:
            # el run se gestiona al inicio de la siguiente vela (stop + timer)
            continue

        # (3) estancamiento: si pasan stale_min sin cambiar de estado, se arma
        # una salida limitada en break-even. Cierra el resto en cuanto el precio
        # toque la entrada (o a mercado si ya está en profit); nunca peor que BE.
        # Mientras siga bajo agua, espera -sostenido solo por el stop de -stop_pct-.
        if not be_armado and c.ts - ultimo_cambio_ts >= stale_ms:
            be_armado = True
        if be_armado:
            alcanza_be = (c.high >= entry.price) if es_long else (c.low <= entry.price)
            if alcanza_be:
                if es_long:
                    precio = c.open if c.open >= entry.price else entry.price
                else:
                    precio = c.open if c.open <= entry.price else entry.price
                cerrar(c.ts, precio, ExitReason.STALE_BE)
                break

    if restante > 0:
        ultima = candles[-1]
        cerrar(ultima.ts, ultima.close, ExitReason.END_OF_DATA)

    return PositionOutcome(
        symbol=entry.symbol, direction=entry.direction, entry_ts=entry.ts,
        entry_price=entry.price, fills=tuple(fills), max_rank=max_rank,
        close_ts=fills[-1].ts,
    )


def _agrupar_por_ventana(
    later: list[TransitionRow], candles: list[CandleRow]
) -> dict[int, list[TransitionRow]]:
    """Asigna cada transición a la vela cuyo minuto [ts, ts+1min) la contiene.
    Transiciones sin vela contenedora (no debería haber con velas contiguas)
    se ignoran."""
    if not candles:
        return {}
    base = candles[0].ts
    fin = candles[-1].ts + MIN_MS
    por_vela: dict[int, list[TransitionRow]] = {}
    for t in later:
        if t.ts < base or t.ts >= fin:
            # Vela con hueco en candles_1m (o transición fuera de la ventana
            # cargada): se descarta en silencio. El riesgo de que esto oculte
            # transiciones reales queda acotado por el guard de retención de
            # run_trajectory (Finding 1), que ya excluye del todo las
            # entradas sin cobertura de velas suficiente.
            continue
        minuto = base + ((t.ts - base) // MIN_MS) * MIN_MS
        por_vela.setdefault(minuto, []).append(t)
    return por_vela
