"""Liquidación económica de una posición y simulación de cartera.

Cada posición se resuelve de forma independiente del margen en `position.py`;
aquí se aplica el margen (para `settle`) y se ejecuta el bucle de eventos que
impone el límite de concurrencia, la unicidad por símbolo y el compuesto.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from scanner_volumen.backtest.trajectory.position import MIN_MS, simulate_position
from scanner_volumen.models import Direction, State
from scanner_volumen.strategy.model import (
    CandleRow, PositionOutcome, StrategyParams, TransitionRow,
)

_WATCH = State.WATCH.rank


def es_entrada(t: TransitionRow) -> bool:
    return t.prev_state.rank < _WATCH <= t.new_state.rank


@dataclass(frozen=True)
class ClosedTrade:
    outcome: PositionOutcome
    margin: float
    notional: float
    size: float
    pnl: float
    fees: float
    fill_pnls: tuple[float, ...]


def settle(outcome: PositionOutcome, margin: float, params: StrategyParams) -> ClosedTrade:
    signo = 1.0 if outcome.direction is Direction.LONG else -1.0
    notional = margin * params.apalancamiento
    size = notional / outcome.entry_price
    fee_entrada = params.comision_taker * notional

    fill_pnls: list[float] = []
    fees = fee_entrada
    for f in outcome.fills:
        size_cerrado = size * f.fraction
        fee_salida = params.comision_taker * size_cerrado * f.price
        bruto = signo * (f.price - outcome.entry_price) * size_cerrado
        fees += fee_salida
        fill_pnls.append(bruto - fee_salida)
    pnl = sum(fill_pnls) - fee_entrada

    return ClosedTrade(
        outcome=outcome, margin=margin, notional=notional, size=size,
        pnl=pnl, fees=fees, fill_pnls=tuple(fill_pnls),
    )


@dataclass(frozen=True)
class TrajectoryRun:
    trades: tuple[ClosedTrade, ...]
    skipped_neutral: int
    skipped_symbol_open: int
    skipped_max_concurrent: int
    skipped_sin_velas: int
    skipped_score_bajo: int
    skipped_congelado: int
    equity_inicial: float
    equity_final: float
    ts_min: int | None
    ts_max: int | None
    total_transitions: int
    max_concurrentes_alcanzado: int
    params: StrategyParams


def run_trajectory(
    transitions: list[TransitionRow],
    candles_for: Callable[[str, int], list[CandleRow]],
    params: StrategyParams,
) -> TrajectoryRun:
    por_simbolo: dict[str, list[TransitionRow]] = {}
    for t in transitions:
        por_simbolo.setdefault(t.symbol, []).append(t)

    # precalcular cada entrada candidata: outcome (independiente del margen)
    # y su close_ts. El PnL escala lineal con el margen, así que basta el
    # outcome para ordenar los cierres en el tiempo. Las NEUTRAL no simulan
    # posición (no ocupan slot) pero se cuentan en el bucle de eventos.
    entradas: list[PositionOutcome] = []
    n_neutral = n_sin_velas = n_score_bajo = 0
    for t in transitions:
        if not es_entrada(t):
            continue
        if t.direction is Direction.NEUTRAL:
            n_neutral += 1
            continue
        if t.score < params.min_score_entrada:
            n_score_bajo += 1
            continue
        posteriores = [u for u in por_simbolo[t.symbol] if u.ts > t.ts]
        velas = candles_for(t.symbol, t.ts)
        # retención: candles_1m se poda (~14 días) pero state_transitions no.
        # Si las velas disponibles del símbolo empiezan bien después de esta
        # entrada, no hay cobertura real y hay que descartarla (no simular,
        # no ocupar slot) en lugar de arriesgarse a un precio erróneo o a
        # que simulate_position reciba una lista vacía.
        if not velas or velas[0].ts > t.ts + MIN_MS:
            n_sin_velas += 1
            continue
        entradas.append(simulate_position(t, posteriores, velas, params))
    entradas.sort(key=lambda o: o.entry_ts)

    balance = params.equity_inicial
    abiertos: dict[str, int] = {}  # symbol -> close_ts
    trades: list[ClosedTrade] = []
    n_symbol = n_concurr = n_congelado = 0
    max_conc = 0

    pendientes: dict[str, ClosedTrade] = {}
    # congelación por racha de pérdidas: racha[symbol] = close_ts de las pérdidas
    # consecutivas recientes; congelado_hasta[symbol] = ts hasta el que no se entra.
    racha: dict[str, list[int]] = {}
    congelado_hasta: dict[str, int] = {}
    ventana_ms = int(params.freeze_ventana_horas * 3_600_000)
    congelar_ms = int(params.freeze_horas * 3_600_000)

    def registrar_resultado(trade: ClosedTrade) -> None:
        """Actualiza la racha de pérdidas del símbolo al cerrar un trade y, si
        se cumplen N pérdidas seguidas dentro de la ventana, congela el par."""
        if params.freeze_perdidas <= 0:
            return
        sym = trade.outcome.symbol
        if trade.pnl < 0:
            r = racha.setdefault(sym, [])
            r.append(trade.outcome.close_ts)
            del r[:-params.freeze_perdidas]  # conserva solo las últimas N
            if (len(r) >= params.freeze_perdidas
                    and r[-1] - r[0] <= ventana_ms):
                congelado_hasta[sym] = trade.outcome.close_ts + congelar_ms
                r.clear()
        else:
            racha[sym] = []  # un no-perdedor rompe la racha

    def cerrar_hasta(ts: int) -> None:
        nonlocal balance
        # cierra y contabiliza, en orden de close_ts, los que hayan vencido en ts
        vencidos = [sym for sym, cts in abiertos.items() if cts <= ts]
        vencidos.sort(key=lambda s: abiertos[s])
        for sym in vencidos:
            trade = pendientes.pop(sym)
            balance += trade.pnl
            trades.append(trade)
            del abiertos[sym]
            registrar_resultado(trade)

    for out in entradas:
        cerrar_hasta(out.entry_ts)
        if congelado_hasta.get(out.symbol, 0) > out.entry_ts:
            n_congelado += 1
            continue
        if out.symbol in abiertos:
            n_symbol += 1
            continue
        if len(abiertos) >= params.max_concurrentes:
            n_concurr += 1
            continue
        margin = params.fraccion_margen * balance
        trade = settle(out, margin, params)
        abiertos[out.symbol] = out.close_ts
        pendientes[out.symbol] = trade
        max_conc = max(max_conc, len(abiertos))

    # flush de los que quedan abiertos, en orden de cierre
    for sym in sorted(pendientes, key=lambda s: abiertos[s]):
        trade = pendientes[sym]
        balance += trade.pnl
        trades.append(trade)

    ts_all = [t.ts for t in transitions]
    return TrajectoryRun(
        trades=tuple(sorted(trades, key=lambda tr: tr.outcome.close_ts)),
        skipped_neutral=n_neutral, skipped_symbol_open=n_symbol,
        skipped_max_concurrent=n_concurr, skipped_sin_velas=n_sin_velas,
        skipped_score_bajo=n_score_bajo, skipped_congelado=n_congelado,
        equity_inicial=params.equity_inicial, equity_final=balance,
        ts_min=min(ts_all, default=None), ts_max=max(ts_all, default=None),
        total_transitions=len(transitions),
        max_concurrentes_alcanzado=max_conc,
        params=params,
    )
