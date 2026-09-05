import pytest

from scanner_volumen.backtest.trajectory.model import (
    CandleRow, ExitReason, TrajectoryParams, TransitionRow,
)
from scanner_volumen.backtest.trajectory.position import simulate_position
from scanner_volumen.models import Direction, State

MIN = 60_000


def tr(ts, prev, new, price, symbol="X", direction=Direction.LONG, score=55.0):
    return TransitionRow(ts=ts, symbol=symbol, prev_state=prev,
                         new_state=new, price=price, direction=direction,
                         score=score)


def velas(inicio, precios):
    # una vela plana por minuto: open=high=low=close=precio
    return [CandleRow(ts=inicio + i * MIN, open=p, high=p, low=p, close=p)
            for i, p in enumerate(precios)]


def test_tras_scale_out_en_profit_el_stop_sube_a_be():
    # LONG entra a 100 (stop original 97.5). Escala a HOT a 110 (en profit) ->
    # el stop del resto sube a break-even (100). El precio vuelve a 100 sin
    # llegar nunca a 97.5: con BE el stop salta en 100; sin BE no saltaría.
    entry = tr(0, State.NORMAL, State.WATCH, 100.0)
    later = [tr(1 * MIN, State.WATCH, State.HOT, 110.0)]
    candles = velas(0, [100, 110, 105, 100, 100])
    out = simulate_position(entry, later, candles, TrajectoryParams())
    assert out.fills[0].reason == ExitReason.SCALE_HOT and out.fills[0].price == 110.0
    assert out.fills[1].reason == ExitReason.STOP
    assert out.fills[1].price == pytest.approx(100.0)  # BE, no 97.5
    assert out.fills[1].ts == 3 * MIN


def test_scale_out_en_perdida_no_mueve_el_stop_a_be():
    # SHORT entra a 100 (stop original 102.5). Escala a HOT a 101 (en PÉRDIDA
    # para un short) -> el stop NO debe moverse a BE. Con precio en 101, un BE
    # erróneo (stop=100) dispararía el stop; el comportamiento correcto no.
    entry = tr(0, State.NORMAL, State.WATCH, 100.0, direction=Direction.SHORT)
    later = [tr(1 * MIN, State.WATCH, State.HOT, 101.0, direction=Direction.SHORT)]
    candles = velas(0, [100, 101, 101])
    out = simulate_position(entry, later, candles, TrajectoryParams())
    reasons = [f.reason for f in out.fills]
    assert ExitReason.STOP not in reasons
    assert reasons[0] == ExitReason.SCALE_HOT
    assert out.fills[-1].reason == ExitReason.END_OF_DATA


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


def test_estancamiento_10min_sin_cambio_sale_en_be():
    # WATCH sin ninguna transición y precio plano en la entrada: a los 10 min se
    # arma la salida en BE y cierra en la entrada (high >= entrada).
    entry = tr(0, State.NORMAL, State.WATCH, 100.0)
    candles = velas(0, [100] * 12)
    out = simulate_position(entry, [], candles, TrajectoryParams())
    assert len(out.fills) == 1
    assert out.fills[0].reason == ExitReason.STALE_BE
    assert out.fills[0].ts == 10 * MIN
    assert out.fills[0].price == pytest.approx(100.0)


def test_estancamiento_en_profit_sale_a_mercado():
    # Estancado pero por encima de la entrada: la salida en BE sale a mercado.
    entry = tr(0, State.NORMAL, State.WATCH, 100.0)
    candles = velas(0, [100] + [105] * 11)  # sube a 105 y se queda
    out = simulate_position(entry, [], candles, TrajectoryParams())
    assert out.fills[0].reason == ExitReason.STALE_BE
    assert out.fills[0].price == pytest.approx(105.0)  # max(mercado, entrada)


def test_estancamiento_bajo_agua_espera_a_be():
    # Estancado y por debajo de la entrada (sin tocar el stop): NO cierra en BE;
    # espera a que el precio vuelva a la entrada. Aquí no vuelve -> fin de datos.
    entry = tr(0, State.NORMAL, State.WATCH, 100.0)
    candles = velas(0, [100] + [99.0] * 15)  # 99 > stop 97.5, pero < entrada
    out = simulate_position(entry, [], candles, TrajectoryParams())
    reasons = [f.reason for f in out.fills]
    assert ExitReason.STALE_BE not in reasons
    assert out.fills[-1].reason == ExitReason.END_OF_DATA


def test_transicion_reinicia_el_timer_de_estancamiento():
    # Una transición a los 6 min reinicia el contador: el estancamiento no se
    # arma a los 10 min desde la entrada, sino a los 6+10=16 (fuera de datos).
    entry = tr(0, State.NORMAL, State.WATCH, 100.0)
    later = [tr(6 * MIN, State.WATCH, State.HOT, 100.0)]  # cambio de estado
    candles = velas(0, [100] * 15)  # datos hasta 14min < 16min
    out = simulate_position(entry, later, candles, TrajectoryParams())
    reasons = [f.reason for f in out.fills]
    assert ExitReason.SCALE_HOT in reasons
    assert ExitReason.STALE_BE not in reasons
    assert out.fills[-1].reason == ExitReason.END_OF_DATA


def test_velas_vacias_lanza_value_error():
    entry = tr(0, State.NORMAL, State.WATCH, 100.0)
    with pytest.raises(ValueError):
        simulate_position(entry, [], [], TrajectoryParams())


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
