from scanner_volumen.backtest.trajectory.model import (
    Direction, ExitReason, Fill, PositionOutcome, TrajectoryParams,
)
from scanner_volumen.backtest.trajectory.portfolio import ClosedTrade, TrajectoryRun
from scanner_volumen.backtest.trajectory.report import format_trajectory_report


def _trade(close_ts, pnl, reason):
    out = PositionOutcome(
        symbol="X", direction=Direction.LONG, entry_ts=0, entry_price=100.0,
        fills=(Fill(ts=close_ts, price=100.0, fraction=1.0, reason=reason),),
        max_rank=1, close_ts=close_ts,
    )
    return ClosedTrade(outcome=out, margin=20.0, notional=400.0, size=4.0,
                       pnl=pnl, fees=0.0, fill_pnls=(pnl,))


def test_informe_incluye_retorno_y_desglose():
    trades = (_trade(1000, 50.0, ExitReason.EXTREME),
              _trade(2000, -30.0, ExitReason.STOP))
    run = TrajectoryRun(
        trades=trades, skipped_neutral=2, skipped_symbol_open=1,
        skipped_max_concurrent=3, equity_inicial=1000.0, equity_final=1020.0,
        ts_min=0, ts_max=2000, params=TrajectoryParams(),
    )
    texto = format_trajectory_report(run)
    assert "1020" in texto           # equity final
    assert "EXTREME" in texto and "STOP" in texto
    assert "NEUTRAL" in texto or "neutral" in texto
