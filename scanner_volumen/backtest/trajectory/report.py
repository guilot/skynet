"""Adaptador del backtest al informe compartido.

Traduce el `TrajectoryRun` de la simulación a la `ResumenOperativa` neutra que
`strategy.report` sabe formatear. Toda la maquetación vive allí, para que el
bot en vivo produzca un informe idéntico sin depender de este paquete.
"""
from __future__ import annotations

from scanner_volumen.backtest.trajectory.portfolio import TrajectoryRun
from scanner_volumen.strategy.model import ResumenOperativa, TradeResumen
from scanner_volumen.strategy.report import format_resumen


def a_resumen(run: TrajectoryRun) -> ResumenOperativa:
    trades = tuple(
        TradeResumen(
            symbol=t.outcome.symbol, direction=t.outcome.direction,
            entry_ts=t.outcome.entry_ts, entry_price=t.outcome.entry_price,
            close_ts=t.outcome.close_ts, fills=t.outcome.fills,
            fill_pnls=t.fill_pnls, margin=t.margin, pnl=t.pnl, fees=t.fees,
            max_rank=t.outcome.max_rank,
        )
        for t in run.trades
    )
    # el orden de este diccionario es el orden de impresión, y reproduce el
    # del informe original
    descartes = {
        "NEUTRAL": run.skipped_neutral,
        "simbolo abierto": run.skipped_symbol_open,
        "tope concurrencia": run.skipped_max_concurrent,
        "sin velas": run.skipped_sin_velas,
        "score bajo": run.skipped_score_bajo,
        "par congelado": run.skipped_congelado,
    }
    return ResumenOperativa(
        titulo="Backtest de trayectoria", trades=trades, descartes=descartes,
        equity_inicial=run.equity_inicial, equity_final=run.equity_final,
        ts_min=run.ts_min, ts_max=run.ts_max,
        total_transiciones=run.total_transitions,
        max_concurrentes_alcanzado=run.max_concurrentes_alcanzado,
    )


def format_trajectory_report(run: TrajectoryRun) -> str:
    return format_resumen(a_resumen(run))
