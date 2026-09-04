"""Simulador de una única posición de trayectoria.

Función pura sobre las transiciones y velas de UN símbolo, independiente del
margen (los `fills` son fracciones del tamaño original). El orden dentro de
cada vela es: (1) stop, (2) transiciones a su ts, (3) time-stop al close;
el stop primero es la elección conservadora del spec.
"""
from __future__ import annotations

from scanner_volumen.backtest.trajectory.model import (
    CandleRow, ExitReason, Fill, PositionOutcome, TrajectoryParams, TransitionRow,
)
from scanner_volumen.models import Direction, State

MIN_MS = 60_000

_HOT = State.HOT.rank
_SIGNAL = State.SIGNAL.rank
_EXTREME = State.EXTREME.rank
_NORMAL = State.NORMAL.rank


def simulate_position(
    entry: TransitionRow,
    later: list[TransitionRow],
    candles: list[CandleRow],
    params: TrajectoryParams,
) -> PositionOutcome:
    es_long = entry.direction is Direction.LONG
    signo = 1.0 if es_long else -1.0
    stop_price = entry.price * (1 - params.stop_pct * signo)

    entry_rank = entry.new_state.rank
    max_rank = entry_rank
    fired_hot = entry_rank >= _HOT      # no se dispara un tramo del nivel de entrada
    fired_signal = entry_rank >= _SIGNAL
    normal_since: int | None = None

    fills: list[Fill] = []
    restante = 1.0
    trans_por_ts = _agrupar_por_ventana(later, candles)
    time_stop_ms = params.time_stop_min * MIN_MS

    def cerrar(ts: float, price: float, reason: ExitReason) -> None:
        nonlocal restante
        if restante <= 0:
            return
        fills.append(Fill(ts=int(ts), price=price, fraction=restante, reason=reason))
        restante = 0.0

    for c in candles:
        if restante <= 0:
            break
        # (1) stop dentro de la vela
        toca_stop = (c.low <= stop_price) if es_long else (c.high >= stop_price)
        if toca_stop:
            paso_al_abrir = (c.open <= stop_price) if es_long else (c.open >= stop_price)
            precio = c.open if paso_al_abrir else stop_price
            cerrar(c.ts, precio, ExitReason.STOP)
            break

        # (2) transiciones cuyo ts cae en [c.ts, c.ts + 1min)
        for t in trans_por_ts.get(c.ts, ()):  # orden ascendente garantizado
            if t.new_state.rank > max_rank:
                max_rank = t.new_state.rank
            # tramos por niveles estrictamente por encima del rank de entrada
            if not fired_hot and max_rank >= _HOT:
                fired_hot = True
                restante -= params.tramo_hot
                fills.append(Fill(ts=t.ts, price=t.price, fraction=params.tramo_hot,
                                  reason=ExitReason.SCALE_HOT))
            if not fired_signal and max_rank >= _SIGNAL:
                fired_signal = True
                restante -= params.tramo_signal
                fills.append(Fill(ts=t.ts, price=t.price, fraction=params.tramo_signal,
                                  reason=ExitReason.SCALE_SIGNAL))
            if max_rank >= _EXTREME:
                cerrar(t.ts, t.price, ExitReason.EXTREME)
                break
            # timer de NORMAL
            if t.new_state.rank == _NORMAL:
                normal_since = t.ts
            elif t.new_state.rank > _NORMAL:
                normal_since = None
        if restante <= 0:
            break

        # (3) time-stop: 30 min en NORMAL sin nueva transición
        if normal_since is not None and c.ts >= normal_since + time_stop_ms:
            cerrar(c.ts, c.close, ExitReason.TIME)
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
            continue
        minuto = base + ((t.ts - base) // MIN_MS) * MIN_MS
        por_vela.setdefault(minuto, []).append(t)
    return por_vela
