from scanner_volumen.backtest.trajectory.model import (
    ExitReason, Fill, TrajectoryParams,
)


def test_params_por_defecto():
    p = TrajectoryParams()
    assert p.equity_inicial == 1000.0
    assert p.fraccion_margen == 0.02
    assert p.apalancamiento == 20.0
    assert p.stop_pct == 0.025
    assert p.max_concurrentes == 5
    assert p.stale_min == 10


def test_fill_lleva_fraccion_y_motivo():
    f = Fill(ts=1000, price=10.0, fraction=0.33, reason=ExitReason.SCALE_HOT)
    assert f.reason is ExitReason.SCALE_HOT
    assert f.fraction == 0.33
