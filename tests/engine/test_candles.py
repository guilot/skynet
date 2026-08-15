# tests/engine/test_candles.py
from scanner_volumen.engine.candles import CandleBuffer
from scanner_volumen.models import Candle

MINUTO = 60_000


def vela(minuto, close=100.0, vol=10.0):
    return Candle(ts=minuto * MINUTO, open=close, high=close, low=close,
                  close=close, base_vol=vol, quote_vol=vol * close)


def test_la_primera_vela_es_nueva():
    buf = CandleBuffer("AAAUSDT")
    assert buf.upsert(vela(1)) is True


def test_reemplazar_la_vela_en_curso_no_cuenta_como_nueva():
    buf = CandleBuffer("AAAUSDT")
    buf.upsert(vela(1, vol=10))
    assert buf.upsert(vela(1, vol=25)) is False
    assert buf.current().base_vol == 25


def test_una_vela_de_minuto_posterior_cierra_la_anterior():
    buf = CandleBuffer("AAAUSDT")
    buf.upsert(vela(1, vol=10))
    buf.upsert(vela(2, vol=5))
    assert [c.ts for c in buf.closed(5)] == [1 * MINUTO]
    assert buf.current().ts == 2 * MINUTO


def test_ignora_velas_mas_antiguas_que_la_actual():
    """Puede ocurrir tras una reconexión con relleno REST desordenado."""
    buf = CandleBuffer("AAAUSDT")
    buf.upsert(vela(5))
    assert buf.upsert(vela(3)) is False
    assert buf.current().ts == 5 * MINUTO


def test_closed_devuelve_las_n_ultimas_en_orden_ascendente():
    buf = CandleBuffer("AAAUSDT")
    for m in range(1, 8):
        buf.upsert(vela(m))
    ultimas = buf.closed(3)
    assert [c.ts for c in ultimas] == [4 * MINUTO, 5 * MINUTO, 6 * MINUTO]


def test_closed_devuelve_menos_de_n_si_no_hay_suficientes():
    buf = CandleBuffer("AAAUSDT")
    buf.upsert(vela(1))
    buf.upsert(vela(2))
    assert len(buf.closed(10)) == 1


def test_close_at_devuelve_el_cierre_de_hace_n_minutos():
    buf = CandleBuffer("AAAUSDT")
    for m in range(1, 11):
        buf.upsert(vela(m, close=100.0 + m))
    # la vela en curso es el minuto 10, con close 110
    assert buf.close_at(0) == 110.0
    assert buf.close_at(5) == 105.0


def test_close_at_devuelve_none_sin_histórico_suficiente():
    buf = CandleBuffer("AAAUSDT")
    buf.upsert(vela(1))
    assert buf.close_at(30) is None


def test_close_at_devuelve_none_si_falta_la_vela_de_ese_minuto():
    """Un hueco en el histórico no debe devolver el cierre de otro minuto."""
    buf = CandleBuffer("AAAUSDT")
    buf.upsert(vela(1, close=100))
    buf.upsert(vela(10, close=110))
    assert buf.close_at(5) is None


def test_session_volume_suma_desde_el_inicio_del_dia():
    buf = CandleBuffer("AAAUSDT")
    for m in range(1, 6):
        buf.upsert(vela(m, vol=10))
    # incluye la vela en curso
    assert buf.session_volume(day_start_ms=0) == 50
    assert buf.session_volume(day_start_ms=3 * MINUTO) == 30


def test_el_buffer_respeta_su_capacidad():
    buf = CandleBuffer("AAAUSDT", capacity=5)
    for m in range(1, 21):
        buf.upsert(vela(m))
    assert len(buf.all_closed()) <= 5
