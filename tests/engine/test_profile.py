import pytest

from scanner_volumen.config import ProfileConfig
from scanner_volumen.engine.profile import (
    build_profile, minute_of_day, rolling_baseline,
)
from scanner_volumen.models import Candle

MINUTO = 60_000
DIA = 1440 * MINUTO


def cfg(**kwargs):
    base = dict(history_days=14, smoothing_window_minutes=7,
                min_days_for_confidence=3, rolling_fallback_candles=120,
                stale_after_hours=24.0)
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
    # Valores exactos de la interpolación lineal (pos = q * (len-1)) sobre
    # las 14 muestras [10, 20, ..., 140]:
    tolerancia = 1e-9
    assert abs(s.median - 75.0) < tolerancia
    assert abs(s.p75 - 107.5) < tolerancia
    assert abs(s.p90 - 127.0) < tolerancia
    assert abs(s.p95 - 133.5) < tolerancia


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


def test_cumulative_baseline_salta_huecos_y_suma_solo_los_slots_con_datos():
    """A diferencia de baseline(), que se niega ante un slot ausente,
    cumulative_baseline salta los huecos y sigue sumando: pin del
    comportamiento tolerante documentado en el docstring."""
    velas = [vela(0 * MINUTO, 100.0), vela(5 * MINUTO, 100.0)]
    perfil = build_profile("AAAUSDT", velas, cfg(smoothing_window_minutes=0))
    assert perfil.baseline(2) is None  # minuto 2 no tiene muestras
    assert perfil.cumulative_baseline(9) == 200.0  # solo minutos 0 y 5 aportan


def test_cumulative_baseline_none_si_no_hay_datos_en_el_rango():
    velas = [vela(500 * MINUTO, 100.0)]
    perfil = build_profile("AAAUSDT", velas, cfg(smoothing_window_minutes=0))
    assert perfil.cumulative_baseline(9) is None  # minutos 0..9 sin datos


def test_rolling_baseline_es_la_mediana_de_las_ultimas_n():
    velas = [vela(m * MINUTO, float(m)) for m in range(1, 11)]
    assert rolling_baseline(velas, 10) == 5.5
    assert rolling_baseline(velas, 4) == 8.5


def test_rolling_baseline_sin_velas_devuelve_none():
    assert rolling_baseline([], 10) is None


def test_rolling_baseline_n_cero_devuelve_none():
    """candles[-0:] en Python es la lista entera, no un slice vacío: n=0
    debe desactivar el fallback devolviendo None, no la mediana de todo
    el histórico."""
    velas = [vela(m * MINUTO, float(m)) for m in range(1, 11)]
    assert rolling_baseline(velas, 0) is None


def test_rolling_baseline_n_negativo_devuelve_none():
    velas = [vela(m * MINUTO, float(m)) for m in range(1, 11)]
    assert rolling_baseline(velas, -3) is None


# --- typical_volume: el número que decide el filtro de libro fino ---

def test_typical_volume_de_un_perfil_uniforme_es_ese_valor():
    velas = dias_sinteticos(14, lambda d, m: 250.0)
    perfil = build_profile("AAAUSDT", velas, cfg(smoothing_window_minutes=0))
    assert perfil.typical_volume() == pytest.approx(250.0)


def test_typical_volume_resiste_un_grupo_minoritario_de_slots_atipicos():
    """Igual que la mediana por slot resiste una vela extrema
    (test_la_mediana_resiste_un_valor_extremo), la mediana agregada de
    typical_volume no debe dejarse arrastrar por una minoria de slots con
    un patron distinto (p. ej. una hora de actividad puntual)."""
    def volumen(d, m):
        return 50_000.0 if m < 60 else 200.0  # 60 de 1440 slots, minoria clara

    velas = dias_sinteticos(14, volumen)
    perfil = build_profile("AAAUSDT", velas, cfg(smoothing_window_minutes=0))
    assert perfil.typical_volume() == pytest.approx(200.0)


def test_typical_volume_none_si_hay_pocos_slots_poblados():
    # una sola vela: un único slot poblado, muy por debajo de la mitad del
    # día que exige MIN_SLOTS_POBLADOS_PARA_TIPICO.
    velas = [vela(100 * MINUTO, 50.0)]
    perfil = build_profile("AAAUSDT", velas, cfg(smoothing_window_minutes=0))
    assert perfil.typical_volume() is None


def test_typical_volume_none_justo_por_debajo_de_medio_dia_de_slots():
    # 600 minutos consecutivos poblados: menos que MIN_SLOTS_POBLADOS_PARA_TIPICO
    # (720, la mitad de 1440), así que sigue sin ser representativo del día.
    velas = [vela(m * MINUTO, 500.0) for m in range(600)]
    perfil = build_profile("AAAUSDT", velas, cfg(smoothing_window_minutes=0))
    assert perfil.typical_volume() is None


def test_typical_volume_no_toca_el_reloj_ni_hace_io():
    """Pin de diseño (engine/ debe seguir siendo puro): typical_volume solo
    lee self.slots, ya calculados por build_profile -- no acepta ningún
    argumento de "ahora" ni de origen de datos."""
    import inspect

    from scanner_volumen.engine.profile import VolumeProfile

    firma = inspect.signature(VolumeProfile.typical_volume)
    assert list(firma.parameters) == ["self"]
