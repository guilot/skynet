from scanner_volumen.config import EngineConfig, ProfileConfig
from scanner_volumen.engine.candles import CandleBuffer
from scanner_volumen.engine.metrics import MetricsBuilder
from scanner_volumen.engine.profile import build_profile
from scanner_volumen.models import Candle, Ticker

MINUTO = 60_000
DIA = 1440 * MINUTO


def vela(ts, close=100.0, vol=100.0):
    return Candle(ts=ts, open=close, high=close, low=close, close=close,
                  base_vol=vol / close, quote_vol=vol)


def perfil_plano(volumen=100.0):
    velas = [vela(d * DIA + m * MINUTO, vol=volumen) for d in range(14) for m in range(1440)]
    return build_profile("AAAUSDT", velas,
                         ProfileConfig(history_days=14, smoothing_window_minutes=0,
                                       min_days_for_confidence=3, rolling_fallback_candles=120))


def constructor():
    return MetricsBuilder(
        EngineConfig(tick_seconds=1.0, ticker_poll_seconds=3.0,
                     live_rvol_min_elapsed_seconds=15, zscore_window_minutes=60),
        ProfileConfig(history_days=14, smoothing_window_minutes=0,
                      min_days_for_confidence=3, rolling_fallback_candles=120),
    )


def ticker(volumen=5e6, cambio=6.4):
    return Ticker(symbol="AAAUSDT", last=110.0, change_24h=cambio,
                  volume_24h_usdt=volumen, open_interest=1000.0,
                  funding_rate=0.0001, ts=0)


def buffer_con_pump():
    """14 dias de base + un tramo final con precio y volumen disparados."""
    buf = CandleBuffer("AAAUSDT")
    base_dia = 14 * DIA
    for m in range(0, 120):
        buf.upsert(vela(base_dia + m * MINUTO, close=100.0, vol=100.0))
    for i, m in enumerate(range(120, 126)):
        buf.upsert(vela(base_dia + m * MINUTO, close=100.0 + i * 2, vol=800.0))
    return buf


def test_calcula_rvol_y_retornos_de_un_pump():
    """Valores exactos (no solo cotas) para que un bug que siga dando ">5"
    o ">0" con un resultado distinto del correcto no pase inadvertido.

    Con perfil plano (baseline=100 en todo minuto) y las 5 ultimas velas
    cerradas (minutos 120-124) a volumen 800:
      rvol_1m_closed = 800 / 100                = 8.0
      rvol_5m        = (800*5) / (100*5)        = 8.0
    El precio pasa de 100.0 (minuto 95, usado por ret_5m y ret_30m) a 110.0
    (vela en curso, minuto 125):
      ret_5m = ret_30m = (110/100 - 1) * 100    = 10.0
    """
    b = constructor()
    buf = buffer_con_pump()
    ahora = buf.current().ts + 60_000
    m = b.compute("AAAUSDT", buf, perfil_plano(), ticker(), market_cap=5e7, now_ms=ahora)
    assert m.rvol_1m_closed is not None and abs(m.rvol_1m_closed - 8.0) < 1e-9
    assert m.rvol_5m is not None and abs(m.rvol_5m - 8.0) < 1e-9
    assert m.ret_5m is not None and abs(m.ret_5m - 10.0) < 1e-9
    # ret_30m se calcula y se guarda aunque Task 10 no lo puntue (ver brief).
    assert m.ret_30m is not None and abs(m.ret_30m - 10.0) < 1e-9


def test_ret_24h_viene_del_ticker():
    b = constructor()
    m = b.compute("AAAUSDT", buffer_con_pump(), perfil_plano(), ticker(cambio=13.8),
                  market_cap=5e7, now_ms=0)
    assert m.ret_24h == 13.8


def test_demand_burst_necesita_historial_de_rvol():
    b = constructor()
    buf = buffer_con_pump()
    ahora = buf.current().ts
    primera = b.compute("AAAUSDT", buf, perfil_plano(), ticker(), 5e7, now_ms=ahora)
    assert primera.demand_burst is None  # aun no hay referencia de hace 5 min

    # compute() usa el ts de la ultima vela cerrada (no `ahora`, que es la
    # vela en curso) como referencia para "hace 5 minutos".
    ultima_cerrada_ts = buf.closed(1)[0].ts
    b.record_rvol("AAAUSDT", 3.1, ts=ultima_cerrada_ts - 5 * MINUTO)
    segunda = b.compute("AAAUSDT", buf, perfil_plano(), ticker(), 5e7, now_ms=ahora)
    # rvol_1m_closed en este instante es 8.0 (ver test del pump); demand_burst
    # debe ser exactamente 8.0 / 3.1, no solo "> 2" (una cota tan floja la
    # pasaria tambien una implementacion que devolviera, p.ej., 8.0/max(3.1,1)).
    esperado = 8.0 / 3.1
    assert segunda.demand_burst is not None and abs(segunda.demand_burst - esperado) < 1e-9


def test_usa_baseline_rolling_cuando_la_confianza_es_baja():
    """Un listing nuevo no tiene perfil fiable, pero debe producir RVOL
    igualmente, usando la mediana rolling de sus propias velas recientes en
    vez del perfil intradia.

    El perfil se construye con volumen 50 (baseline de perfil en el minuto
    129 = 50), mientras que las velas cerradas del buffer llevan volumen 100
    (mediana rolling = 100). Si la implementacion ignorase la confianza baja
    y usara igualmente profile.baseline(), el resultado seria 100/50 = 2.0
    en vez de 100/100 = 1.0: los dos caminos dan valores distintos a
    proposito para que el test los distinga.
    """
    b = constructor()
    velas_pocas = [vela(m * MINUTO, vol=50.0) for m in range(200)]
    perfil_corto = build_profile(
        "AAAUSDT", velas_pocas,
        ProfileConfig(history_days=14, smoothing_window_minutes=0,
                      min_days_for_confidence=3, rolling_fallback_candles=120),
    )
    assert perfil_corto.confidence == "low"
    assert perfil_corto.baseline(129) == 50.0  # verifica la premisa del test

    buf = CandleBuffer("AAAUSDT")
    for m in range(0, 130):
        buf.upsert(vela(m * MINUTO, vol=100.0))
    buf.upsert(vela(130 * MINUTO, vol=900.0))

    m = b.compute("AAAUSDT", buf, perfil_corto, ticker(), 5e7, now_ms=131 * MINUTO)
    assert m.profile_confidence == "low"
    assert m.rvol_1m_closed is not None and abs(m.rvol_1m_closed - 1.0) < 1e-9


def test_rvol_hace_elige_el_mas_cercano_no_el_primero_ni_el_ultimo():
    """Registra tres valores de RVOL dentro de la tolerancia de +-1 minuto
    alrededor del objetivo (hace 5 minutos): uno antes, uno exacto y uno
    despues. El exacto tiene distancia 0 y debe ser el que use demand_burst,
    no el primero registrado (antes) ni el ultimo (despues) -- eso descarta
    una regresion de "mas cercano" a "primer match" o "ultimo match" en el
    bucle de _rvol_hace.
    """
    b = constructor()
    buf = buffer_con_pump()
    ahora = buf.current().ts  # rvol_1m_closed en este instante es 8.0 (ver pump)
    # compute() usa el ts de la ultima vela cerrada (no `ahora`, que es la
    # vela en curso) como referencia para "hace 5 minutos".
    ultima_cerrada_ts = buf.closed(1)[0].ts
    objetivo = ultima_cerrada_ts - 5 * MINUTO

    b.record_rvol("AAAUSDT", 2.0, ts=objetivo - 30_000)  # antes: primero registrado
    b.record_rvol("AAAUSDT", 5.0, ts=objetivo)  # exacto: el mas cercano (distancia 0)
    b.record_rvol("AAAUSDT", 9.0, ts=objetivo + 30_000)  # despues: ultimo registrado

    m = b.compute("AAAUSDT", buf, perfil_plano(), ticker(), 5e7, now_ms=ahora)
    esperado = 8.0 / 5.0  # rvol_cerrado(8.0) / rvol_hace_mas_cercano(5.0) = 1.6
    assert m.demand_burst is not None and abs(m.demand_burst - esperado) < 1e-9


def test_rvol_session_es_none_con_confianza_baja_sin_fallback_rolling():
    """A diferencia de rvol_1m/rvol_5m, rvol_session NO recurre al baseline
    rolling cuando la confianza del perfil es baja: no existe sesion
    historica con la que comparar el volumen acumulado de un simbolo nuevo.
    Es una decision deliberada (ver comentario en compute()), no un bug
    pendiente de arreglar, y este test fija ese comportamiento.
    """
    b = constructor()
    velas_pocas = [vela(m * MINUTO, vol=50.0) for m in range(200)]
    perfil_corto = build_profile(
        "AAAUSDT", velas_pocas,
        ProfileConfig(history_days=14, smoothing_window_minutes=0,
                      min_days_for_confidence=3, rolling_fallback_candles=120),
    )
    assert perfil_corto.confidence == "low"

    buf = CandleBuffer("AAAUSDT")
    for m in range(0, 130):
        buf.upsert(vela(m * MINUTO, vol=100.0))
    buf.upsert(vela(130 * MINUTO, vol=900.0))

    m = b.compute("AAAUSDT", buf, perfil_corto, ticker(), 5e7, now_ms=131 * MINUTO)
    assert m.profile_confidence == "low"
    assert m.rvol_1m_closed is not None
    assert m.rvol_session is None


def test_el_vwap_se_calcula_solo_sobre_el_dia_utc_en_curso():
    b = constructor()
    buf = CandleBuffer("AAAUSDT")
    # dia anterior a precio 10, dia actual a precio 100
    buf.upsert(vela(1400 * MINUTO, close=10.0, vol=1000.0))
    buf.upsert(vela(1441 * MINUTO, close=100.0, vol=1000.0))
    buf.upsert(vela(1442 * MINUTO, close=100.0, vol=1000.0))
    m = b.compute("AAAUSDT", buf, perfil_plano(), ticker(), 5e7, now_ms=1443 * MINUTO)
    assert m.vwap is not None and abs(m.vwap - 100.0) < 1e-6


def test_metricas_sin_datos_no_lanzan_excepcion():
    b = constructor()
    vacio = CandleBuffer("AAAUSDT")
    m = b.compute("AAAUSDT", vacio, perfil_plano(), ticker(), None, now_ms=0)
    assert m.rvol_1m_closed is None
    assert m.ret_5m is None
    assert m.market_cap is None
