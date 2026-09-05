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
    assert intents[0].reason is ExitReason.STOP
    assert intents[0].precio_referencia == pytest.approx(102.5)


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
