"""Formatea el resultado del backtest de trayectoria en texto plano."""
from __future__ import annotations

from datetime import datetime, timezone

from scanner_volumen.backtest.trajectory.model import ExitReason
from scanner_volumen.backtest.trajectory.portfolio import TrajectoryRun


def _fecha(ms: int | None) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _drawdown_pct(run: TrajectoryRun) -> float:
    balance = run.equity_inicial
    pico = balance
    peor = 0.0
    for t in sorted(run.trades, key=lambda tr: tr.outcome.close_ts):
        balance += t.pnl
        pico = max(pico, balance)
        if pico > 0:
            peor = min(peor, (balance - pico) / pico)
    return peor * 100.0


def format_trajectory_report(run: TrajectoryRun) -> str:
    lineas: list[str] = []
    lineas.append("== Backtest de trayectoria ==")
    lineas.append(f"Ventana: {_fecha(run.ts_min)} -> {_fecha(run.ts_max)} (UTC)")
    lineas.append("")
    lineas.append(f"Trades ejecutados: {len(run.trades)}")
    lineas.append(f"  descartes NEUTRAL:          {run.skipped_neutral}")
    lineas.append(f"  descartes simbolo abierto:  {run.skipped_symbol_open}")
    lineas.append(f"  descartes tope concurrencia:{run.skipped_max_concurrent}")
    lineas.append("")

    ret = ((run.equity_final / run.equity_inicial) - 1) * 100 if run.equity_inicial else 0.0
    lineas.append(f"Equity: {run.equity_inicial:.2f} -> {run.equity_final:.2f} "
                  f"({ret:+.2f}%)")
    lineas.append(f"Drawdown maximo: {_drawdown_pct(run):.2f}%")
    lineas.append("")

    ganadores = [t for t in run.trades if t.pnl > 0]
    perdedores = [t for t in run.trades if t.pnl <= 0]
    n = len(run.trades)
    win_rate = (len(ganadores) / n * 100) if n else 0.0
    media_g = (sum(t.pnl for t in ganadores) / len(ganadores)) if ganadores else 0.0
    media_p = (sum(t.pnl for t in perdedores) / len(perdedores)) if perdedores else 0.0
    lineas.append(f"Win rate: {win_rate:.1f}%  ({len(ganadores)}/{n})")
    lineas.append(f"Media ganancia: {media_g:+.2f}   Media perdida: {media_p:+.2f}")
    lineas.append("")

    por_motivo: dict[str, float] = {r.value: 0.0 for r in ExitReason}
    for t in run.trades:
        for fill, pnl in zip(t.outcome.fills, t.fill_pnls):
            por_motivo[fill.reason.value] += pnl
    lineas.append("PnL por motivo de salida:")
    for motivo, pnl in por_motivo.items():
        lineas.append(f"  {motivo:<12} {pnl:+.2f}")

    return "\n".join(lineas)
