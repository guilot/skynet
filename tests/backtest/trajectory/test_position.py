import pytest

from scanner_volumen.backtest.trajectory.model import (
    CandleRow, ExitReason, TrajectoryParams, TransitionRow,
)
from scanner_volumen.backtest.trajectory.position import simulate_position
from scanner_volumen.models import Direction, State

MIN = 60_000


def tr(ts, prev, new, price, symbol="X", direction=Direction.LONG):
    return TransitionRow(ts=ts, symbol=symbol, prev_state=prev,
                         new_state=new, price=price, direction=direction)


def velas(inicio, precios):
    # una vela plana por minuto: open=high=low=close=precio
    return [CandleRow(ts=inicio + i * MIN, open=p, high=p, low=p, close=p)
            for i, p in enumerate(precios)]


def test_escalera_completa_dispara_los_tres_tramos():
    entry = tr(0, State.NORMAL, State.WATCH, 100.0)
    later = [
        tr(1 * MIN, State.WATCH, State.HOT, 110.0),
        tr(2 * MIN, State.HOT, State.SIGNAL, 120.0),
        tr(3 * MIN, State.SIGNAL, State.EXTREME, 130.0),
    ]
    candles = velas(0, [100, 110, 120, 130, 130])
    out = simulate_position(entry, later, candles, TrajectoryParams())
    reasons = [f.reason for f in out.fills]
    assert reasons == [ExitReason.SCALE_HOT, ExitReason.SCALE_SIGNAL, ExitReason.EXTREME]
    assert out.fills[0].price == 110.0 and out.fills[0].fraction == pytest.approx(0.33)
    assert out.fills[1].price == 120.0 and out.fills[1].fraction == pytest.approx(0.33)
    assert out.fills[2].price == 130.0 and out.fills[2].fraction == pytest.approx(0.34)
    assert sum(f.fraction for f in out.fills) == pytest.approx(1.0)
    assert out.close_ts == 3 * MIN
    assert out.max_rank == State.EXTREME.rank


def test_salto_a_signal_acumula_tramos_hot_y_signal():
    entry = tr(0, State.NORMAL, State.WATCH, 100.0)
    later = [tr(1 * MIN, State.WATCH, State.SIGNAL, 120.0)]
    candles = velas(0, [100, 120, 120, 120])
    out = simulate_position(entry, later, candles, TrajectoryParams())
    # ambos tramos al mismo precio/ts, luego cierra por fin de datos
    assert [f.reason for f in out.fills][:2] == [
        ExitReason.SCALE_HOT, ExitReason.SCALE_SIGNAL]
    assert out.fills[0].price == 120.0 and out.fills[1].price == 120.0
    assert out.fills[-1].reason == ExitReason.END_OF_DATA
    assert sum(f.fraction for f in out.fills) == pytest.approx(1.0)


def test_stop_long_cierra_al_precio_de_stop():
    entry = tr(0, State.NORMAL, State.WATCH, 100.0)
    # vela con low por debajo del stop (100*0.975=97.5)
    candles = [CandleRow(ts=0, open=100, high=100, low=100, close=100),
               CandleRow(ts=MIN, open=99, high=99, low=97.0, close=98)]
    out = simulate_position(entry, [], candles, TrajectoryParams())
    assert len(out.fills) == 1
    assert out.fills[0].reason == ExitReason.STOP
    assert out.fills[0].price == pytest.approx(97.5)
    assert out.close_ts == MIN


def test_stop_long_con_hueco_rellena_al_open():
    entry = tr(0, State.NORMAL, State.WATCH, 100.0)
    candles = [CandleRow(ts=0, open=100, high=100, low=100, close=100),
               CandleRow(ts=MIN, open=96.0, high=96.0, low=95.0, close=95.5)]
    out = simulate_position(entry, [], candles, TrajectoryParams())
    assert out.fills[0].reason == ExitReason.STOP
    assert out.fills[0].price == pytest.approx(96.0)  # open, no 97.5


def test_time_stop_tras_30min_en_normal():
    entry = tr(0, State.NORMAL, State.WATCH, 100.0)
    later = [tr(1 * MIN, State.WATCH, State.NORMAL, 100.0)]
    # precios planos a 100 (no toca stop). NORMAL desde t=1min; salida a 1+30
    candles = velas(0, [100] * 33)
    out = simulate_position(entry, later, candles, TrajectoryParams())
    assert len(out.fills) == 1
    assert out.fills[0].reason == ExitReason.TIME
    assert out.fills[0].ts == 31 * MIN  # primera vela con ts >= 1min + 30min
    assert out.fills[0].price == 100.0


def test_timer_normal_se_cancela_al_volver_a_watch():
    entry = tr(0, State.NORMAL, State.WATCH, 100.0)
    later = [
        tr(1 * MIN, State.WATCH, State.NORMAL, 100.0),
        tr(10 * MIN, State.NORMAL, State.WATCH, 100.0),  # cancela antes de 31min
    ]
    candles = velas(0, [100] * 33)
    out = simulate_position(entry, later, candles, TrajectoryParams())
    # sin time-stop: cierra por fin de datos en la última vela
    assert out.fills[-1].reason == ExitReason.END_OF_DATA
    assert out.close_ts == 32 * MIN


def test_entrada_en_hot_no_dispara_tramo_hot():
    entry = tr(0, State.NORMAL, State.HOT, 100.0)  # entra ya en HOT
    later = [tr(1 * MIN, State.HOT, State.SIGNAL, 110.0)]
    candles = velas(0, [100, 110, 110])
    out = simulate_position(entry, later, candles, TrajectoryParams())
    # solo el tramo SIGNAL (33%), luego fin de datos con el resto
    reasons = [f.reason for f in out.fills]
    assert ExitReason.SCALE_HOT not in reasons
    assert reasons[0] == ExitReason.SCALE_SIGNAL
    assert out.fills[0].fraction == pytest.approx(0.33)
