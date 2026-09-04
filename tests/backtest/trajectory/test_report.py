from scanner_volumen.backtest.trajectory.model import (
    Direction, ExitReason, Fill, PositionOutcome, TrajectoryParams,
)
from scanner_volumen.backtest.trajectory.portfolio import ClosedTrade, TrajectoryRun
from scanner_volumen.backtest.trajectory.report import format_trajectory_report
from scanner_volumen.models import State


def _trade(close_ts, pnl, reason, max_rank=1):
    out = PositionOutcome(
        symbol="X", direction=Direction.LONG, entry_ts=0, entry_price=100.0,
        fills=(Fill(ts=close_ts, price=100.0, fraction=1.0, reason=reason),),
        max_rank=max_rank, close_ts=close_ts,
    )
    return ClosedTrade(outcome=out, margin=20.0, notional=400.0, size=4.0,
                       pnl=pnl, fees=0.0, fill_pnls=(pnl,))


def _run(trades, **overrides):
    base = dict(
        trades=trades, skipped_neutral=2, skipped_symbol_open=1,
        skipped_max_concurrent=3, skipped_sin_velas=0,
        equity_inicial=1000.0, equity_final=1020.0,
        ts_min=0, ts_max=2000, total_transitions=5,
        max_concurrentes_alcanzado=1, params=TrajectoryParams(),
    )
    base.update(overrides)
    return TrajectoryRun(**base)


def test_informe_incluye_retorno_y_desglose():
    trades = (_trade(1000, 50.0, ExitReason.EXTREME),
              _trade(2000, -30.0, ExitReason.STOP))
    run = _run(trades)
    texto = format_trajectory_report(run)
    assert "1020" in texto           # equity final
    assert "EXTREME" in texto and "STOP" in texto
    assert "NEUTRAL" in texto or "neutral" in texto
    assert "5 transiciones" in texto  # transitions count


def test_informe_incluye_descartes_sin_velas():
    run = _run((), skipped_sin_velas=7)
    texto = format_trajectory_report(run)
    assert "7" in texto
    assert "sin velas" in texto.lower()


def test_informe_incluye_runners_vs_arrastre_y_concurrencia():
    trades = (
        # runner: alcanza SIGNAL
        _trade(1000, 100.0, ExitReason.EXTREME, max_rank=State.SIGNAL.rank),
        # arrastre: no pasa de HOT
        _trade(2000, -20.0, ExitReason.STOP, max_rank=State.HOT.rank),
        # arrastre: entra y sale en WATCH sin escalar
        _trade(3000, -5.0, ExitReason.TIME, max_rank=State.WATCH.rank),
    )
    run = _run(trades, max_concurrentes_alcanzado=4)
    texto = format_trajectory_report(run)
    assert "Runners" in texto
    assert "Arrastre" in texto
    assert "+100.00" in texto  # pnl runners
    assert "-25.00" in texto  # pnl arrastre acumulado
    assert "Concurrencia" in texto
    assert "4" in texto


def test_informe_separa_cierres_por_fin_de_datos():
    trades = (
        _trade(1000, 50.0, ExitReason.EXTREME),
        _trade(2000, 8.0, ExitReason.END_OF_DATA),
        _trade(3000, -3.0, ExitReason.END_OF_DATA),
    )
    run = _run(trades)
    texto = format_trajectory_report(run)
    assert "fin de datos" in texto.lower()
    assert "+5.00" in texto  # 8 + (-3) pnl de los dos cierres por fin de datos
    # el total de trades (3) sigue incluyendo los de fin de datos
    assert "Trades ejecutados: 3" in texto
