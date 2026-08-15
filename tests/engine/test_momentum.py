# tests/engine/test_momentum.py
from scanner_volumen.engine.candles import CandleBuffer
from scanner_volumen.engine.momentum import pct_return, returns, vwap, vwap_distance
from scanner_volumen.models import Candle

MINUTO = 60_000


def vela(m, close=100.0, high=None, low=None, vol=10.0):
    return Candle(ts=m * MINUTO, open=close, high=high or close, low=low or close,
                  close=close, base_vol=vol, quote_vol=vol * close)


def test_pct_return_calcula_porcentaje():
    assert abs(pct_return(110.0, 100.0) - 10.0) < 1e-9


def test_pct_return_negativo():
    assert abs(pct_return(90.0, 100.0) + 10.0) < 1e-9


def test_pct_return_sin_referencia_devuelve_none():
    assert pct_return(110.0, None) is None


def test_pct_return_con_referencia_cero_devuelve_none():
    assert pct_return(110.0, 0.0) is None


def test_returns_calcula_todos_los_horizontes():
    buf = CandleBuffer("AAAUSDT")
    for m in range(0, 61):
        buf.upsert(vela(m, close=100.0 + m))
    r = returns(buf, horizons=(1, 5, 15, 60))
    # la vela en curso es el minuto 60 con close 160
    assert abs(r[1] - (160 / 159 - 1) * 100) < 1e-9
    assert abs(r[5] - (160 / 155 - 1) * 100) < 1e-9
    assert abs(r[60] - (160 / 100 - 1) * 100) < 1e-9


def test_returns_devuelve_none_en_horizontes_sin_datos():
    buf = CandleBuffer("AAAUSDT")
    buf.upsert(vela(0, close=100.0))
    buf.upsert(vela(1, close=101.0))
    r = returns(buf, horizons=(1, 60))
    assert r[1] is not None
    assert r[60] is None


def test_vwap_pondera_por_volumen():
    velas = [
        vela(0, close=100.0, high=102.0, low=98.0, vol=10.0),   # TP=100, quote=1000
        vela(1, close=200.0, high=200.0, low=200.0, vol=10.0),  # TP=200, quote=2000
    ]
    # (100*1000 + 200*2000) / 3000 = 166.67
    assert abs(vwap(velas) - 166.6667) < 0.001


def test_vwap_usa_precio_tipico_no_el_cierre():
    velas = [vela(0, close=110.0, high=120.0, low=80.0, vol=10.0)]
    # TP = (120+80+110)/3 = 103.333, distinto del cierre (110): si la
    # implementación usara el cierre en vez del precio típico, este test
    # fallaría.
    assert abs(vwap(velas) - 103.3333) < 0.001


def test_vwap_sin_volumen_devuelve_none():
    velas = [vela(0, close=100.0, vol=0.0)]
    assert vwap(velas) is None


def test_vwap_sin_velas_devuelve_none():
    assert vwap([]) is None


def test_vwap_distance_en_porcentaje():
    assert abs(vwap_distance(4.82, 4.63) - 4.1037) < 0.001


def test_vwap_distance_sin_vwap_devuelve_none():
    assert vwap_distance(4.82, None) is None
