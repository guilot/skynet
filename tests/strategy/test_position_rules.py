import pytest

from scanner_volumen.models import Direction, State
from scanner_volumen.strategy.model import (
    CandleRow, ExitReason, Fill, StrategyParams, TransitionRow,
)
from scanner_volumen.strategy.position import PositionRules

MIN = 60_000


def tr(ts, prev, new, price, symbol="X", direction=Direction.LONG, score=75.0):
    return TransitionRow(ts=ts, symbol=symbol, prev_state=prev, new_state=new,
                         price=price, direction=direction, score=score)


def vela(ts, precio):
    return CandleRow(ts=ts, open=precio, high=precio, low=precio, close=precio)


def entrada_long():
    return tr(0, State.NORMAL, State.WATCH, 100.0)


def test_posicion_nueva_no_esta_cerrada():
    r = PositionRules(entrada_long(), StrategyParams())
    assert not r.cerrada
    assert r.restante == pytest.approx(1.0)
    assert r.max_rank == State.WATCH.rank


def test_sin_eventos_relevantes_no_emite_nada():
    r = PositionRules(entrada_long(), StrategyParams())
    assert r.on_candle(vela(MIN, 100.0)) == []


def test_stop_emite_cierre_total_en_el_nivel():
    # LONG entra a 100 -> stop en 97.5. Vela que lo toca intravela con open
    # por encima: la referencia es el nivel del stop, no el open.
    r = PositionRules(entrada_long(), StrategyParams())
    c = CandleRow(ts=MIN, open=99.0, high=99.0, low=97.0, close=98.0)
    intents = r.on_candle(c)
    assert len(intents) == 1
    assert intents[0].reason is ExitReason.STOP
    assert intents[0].fraction == pytest.approx(1.0)
    assert intents[0].precio_referencia == pytest.approx(97.5)
    assert intents[0].ts == MIN


def test_stop_con_hueco_referencia_el_open():
    r = PositionRules(entrada_long(), StrategyParams())
    c = CandleRow(ts=MIN, open=96.0, high=96.0, low=95.0, close=95.5)
    intents = r.on_candle(c)
    assert intents[0].precio_referencia == pytest.approx(96.0)


def test_stop_de_short_usa_el_high():
    entry = tr(0, State.NORMAL, State.WATCH, 100.0, direction=Direction.SHORT)
    r = PositionRules(entry, StrategyParams())
    c = CandleRow(ts=MIN, open=101.0, high=103.0, low=101.0, close=102.0)
    intents = r.on_candle(c)
    assert len(intents) == 1
    assert intents[0].reason is ExitReason.STOP
    assert intents[0].fraction == pytest.approx(1.0)
    assert intents[0].precio_referencia == pytest.approx(102.5)
    assert intents[0].ts == MIN


def test_stop_de_short_con_hueco_referencia_el_open():
    entry = tr(0, State.NORMAL, State.WATCH, 100.0, direction=Direction.SHORT)
    r = PositionRules(entry, StrategyParams())
    c = CandleRow(ts=MIN, open=104.0, high=105.0, low=104.0, close=104.5)
    intents = r.on_candle(c)
    assert intents[0].precio_referencia == pytest.approx(104.0)


def test_confirmar_el_fill_cierra_la_posicion():
    r = PositionRules(entrada_long(), StrategyParams())
    c = CandleRow(ts=MIN, open=99.0, high=99.0, low=97.0, close=98.0)
    intent = r.on_candle(c)[0]
    r.on_fill(Fill(ts=intent.ts, price=intent.precio_referencia,
                   fraction=intent.fraction, reason=intent.reason))
    assert r.cerrada
    assert r.restante == pytest.approx(0.0)
    assert r.on_candle(vela(2 * MIN, 90.0)) == []


def test_evento_con_intencion_sin_confirmar_falla():
    r = PositionRules(entrada_long(), StrategyParams())
    c = CandleRow(ts=MIN, open=99.0, high=99.0, low=97.0, close=98.0)
    r.on_candle(c)  # emite el stop y NO se confirma
    with pytest.raises(ValueError, match="sin confirmar"):
        r.on_candle(vela(2 * MIN, 98.0))


def test_fill_que_no_corresponde_a_ninguna_intencion_falla():
    r = PositionRules(entrada_long(), StrategyParams())
    with pytest.raises(ValueError, match="no corresponde"):
        r.on_fill(Fill(ts=MIN, price=97.5, fraction=1.0, reason=ExitReason.STOP))


def _confirmar(r, intents, precio=None):
    """Ejecuta cada intención al precio de referencia (o a `precio` si se da,
    para simular slippage) y se la confirma al motor."""
    for i in intents:
        r.on_fill(Fill(ts=i.ts, price=precio if precio is not None else i.precio_referencia,
                       fraction=i.fraction, reason=i.reason))


def test_transicion_a_hot_emite_el_tramo():
    r = PositionRules(entrada_long(), StrategyParams())
    t = tr(MIN, State.WATCH, State.HOT, 110.0)
    intents = r.on_candle(vela(MIN, 110.0), [t])
    assert len(intents) == 1
    assert intents[0].reason is ExitReason.SCALE_HOT
    assert intents[0].fraction == pytest.approx(0.33)
    assert intents[0].precio_referencia == pytest.approx(110.0)
    assert intents[0].ts == MIN
    assert r.max_rank == State.HOT.rank


def test_salto_a_signal_acumula_los_dos_tramos():
    r = PositionRules(entrada_long(), StrategyParams())
    t = tr(MIN, State.WATCH, State.SIGNAL, 120.0)
    intents = r.on_candle(vela(MIN, 120.0), [t])
    assert [i.reason for i in intents] == [
        ExitReason.SCALE_HOT, ExitReason.SCALE_SIGNAL]
    assert all(i.precio_referencia == pytest.approx(120.0) for i in intents)


def test_entrada_ya_en_hot_no_cobra_el_tramo_hot():
    # los tramos solo se disparan por niveles ESTRICTAMENTE por encima del
    # rank de entrada
    entry = tr(0, State.NORMAL, State.HOT, 100.0)
    r = PositionRules(entry, StrategyParams())
    intents = r.on_candle(vela(MIN, 110.0), [tr(MIN, State.HOT, State.HOT, 110.0)])
    assert intents == []


def test_parcial_en_beneficio_sube_el_stop_a_be():
    r = PositionRules(entrada_long(), StrategyParams())
    intents = r.on_candle(vela(MIN, 110.0), [tr(MIN, State.WATCH, State.HOT, 110.0)])
    _confirmar(r, intents)
    # el stop original era 97.5; ahora es 100 (BE): una vela a 100 lo toca
    siguiente = r.on_candle(vela(2 * MIN, 100.0))
    assert siguiente[0].reason is ExitReason.STOP
    assert siguiente[0].precio_referencia == pytest.approx(100.0)
    assert siguiente[0].fraction == pytest.approx(0.67)


def test_parcial_en_perdida_no_sube_el_stop_a_be():
    # SHORT que escala a HOT a 101: para un short eso es PÉRDIDA
    entry = tr(0, State.NORMAL, State.WATCH, 100.0, direction=Direction.SHORT)
    r = PositionRules(entry, StrategyParams())
    t = tr(MIN, State.WATCH, State.HOT, 101.0, direction=Direction.SHORT)
    _confirmar(r, r.on_candle(vela(MIN, 101.0), [t]))
    # con BE erróneo (stop=100) una vela a 101 dispararía el stop; no debe
    assert r.on_candle(vela(2 * MIN, 101.0)) == []


def test_el_be_usa_el_precio_ejecutado_no_el_de_referencia():
    """El caso que justifica el protocolo de dos fases: la regla dice que la
    parcial sale a 110 (beneficio), pero el broker la ejecuta a 99 por
    slippage. Con 99 la parcial fue en PÉRDIDA, así que el stop NO sube a BE."""
    r = PositionRules(entrada_long(), StrategyParams())
    intents = r.on_candle(vela(MIN, 110.0), [tr(MIN, State.WATCH, State.HOT, 110.0)])
    _confirmar(r, intents, precio=99.0)
    # si el stop hubiera subido a BE (100), esta vela a 100 lo dispararía
    assert r.on_candle(vela(2 * MIN, 100.0)) == []
