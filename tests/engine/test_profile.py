from scanner_volumen.config import ProfileConfig
from scanner_volumen.engine.profile import (
    build_profile, minute_of_day, rolling_baseline,
)
from scanner_volumen.models import Candle

MINUTO = 60_000
DIA = 1440 * MINUTO


def cfg(**kwargs):
    base = dict(history_days=14, smoothing_window_minutes=7,
                min_days_for_confidence=3, rolling_fallback_candles=120)
    base.update(kwargs)
    return ProfileConfig(**base)


def vela(ts, vol):
    return Candle(ts=ts, open=1, high=1, low=1, close=1, base_vol=vol, quote_vol=vol)


def dias_sinteticos(n_dias, volumen_por_minuto):
    """Genera n_dias completos donde el volumen depende del minuto del día."""
    velas = []
    for d in range(n_dias):
        for m in range(1440):
            velas.append(vela(d * DIA + m * MINUTO, volumen_por_minuto(d, m)))
    return velas


def test_minute_of_day_cuenta_desde_medianoche_utc():
    assert minute_of_day(0) == 0
    assert minute_of_day(90 * MINUTO) == 90
    assert minute_of_day(DIA + 90 * MINUTO) == 90


def test_la_mediana_resiste_un_valor_extremo():
    """Es la razón de usar mediana y no media: una vela extrema no debe
    destruir la referencia."""
    velas = dias_sinteticos(14, lambda d, m: 5000.0 if d == 7 else 100.0)
    perfil = build_profile("AAAUSDT", velas, cfg(smoothing_window_minutes=0))
    assert perfil.baseline(500) == 100.0


def test_captura_el_ciclo_horario():
    def volumen(d, m):
        return 1000.0 if 480 <= m < 600 else 100.0

    velas = dias_sinteticos(14, volumen)
    perfil = build_profile("AAAUSDT", velas, cfg(smoothing_window_minutes=0))
    assert perfil.baseline(500) == 1000.0
    assert perfil.baseline(100) == 100.0


def test_el_suavizado_amplia_el_numero_de_muestras():
    velas = dias_sinteticos(14, lambda d, m: 100.0)
    sin_suavizar = build_profile("AAAUSDT", velas, cfg(smoothing_window_minutes=0))
    suavizado = build_profile("AAAUSDT", velas, cfg(smoothing_window_minutes=7))
    assert sin_suavizar.slots[500].samples == 14
    assert suavizado.slots[500].samples == 14 * 15


def test_el_suavizado_da_la_vuelta_a_medianoche():
    """El minuto 0 debe tomar muestras del minuto 1439 del día anterior."""
    velas = dias_sinteticos(14, lambda d, m: 100.0)
    perfil = build_profile("AAAUSDT", velas, cfg(smoothing_window_minutes=7))
    assert perfil.slots[0].samples == 14 * 15


def test_calcula_percentiles():
    velas = dias_sinteticos(14, lambda d, m: float(d + 1) * 10)
    perfil = build_profile("AAAUSDT", velas, cfg(smoothing_window_minutes=0))
    s = perfil.slots[100]
    assert s.median <= s.p75 <= s.p90 <= s.p95
    assert s.p95 >= 130


def test_confianza_baja_con_poco_historico():
    velas = dias_sinteticos(2, lambda d, m: 100.0)
    perfil = build_profile("AAAUSDT", velas, cfg(min_days_for_confidence=3))
    assert perfil.confidence == "low"


def test_confianza_alta_con_historico_suficiente():
    velas = dias_sinteticos(14, lambda d, m: 100.0)
    perfil = build_profile("AAAUSDT", velas, cfg(min_days_for_confidence=3))
    assert perfil.confidence == "high"


def test_slot_sin_muestras_devuelve_none():
    velas = [vela(100 * MINUTO, 50.0)]
    perfil = build_profile("AAAUSDT", velas, cfg(smoothing_window_minutes=0))
    assert perfil.baseline(800) is None


def test_baseline_cero_devuelve_none():
    """Un símbolo ilíquido puede tener volumen cero en todo un slot; devolver
    0.0 haría explotar el RVOL al dividir."""
    velas = dias_sinteticos(14, lambda d, m: 0.0)
    perfil = build_profile("AAAUSDT", velas, cfg(smoothing_window_minutes=0))
    assert perfil.baseline(500) is None


def test_cumulative_baseline_suma_desde_medianoche():
    velas = dias_sinteticos(14, lambda d, m: 100.0)
    perfil = build_profile("AAAUSDT", velas, cfg(smoothing_window_minutes=0))
    assert perfil.cumulative_baseline(9) == 1000.0  # minutos 0..9


def test_rolling_baseline_es_la_mediana_de_las_ultimas_n():
    velas = [vela(m * MINUTO, float(m)) for m in range(1, 11)]
    assert rolling_baseline(velas, 10) == 5.5
    assert rolling_baseline(velas, 4) == 8.5


def test_rolling_baseline_sin_velas_devuelve_none():
    assert rolling_baseline([], 10) is None
