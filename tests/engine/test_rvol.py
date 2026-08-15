from scanner_volumen.config import ProfileConfig
from scanner_volumen.engine.profile import build_profile
from scanner_volumen.engine.rvol import (
    rvol_closed, rvol_live, rvol_session, rvol_window,
)
from scanner_volumen.models import Candle

MINUTO = 60_000
DIA = 1440 * MINUTO


def vela(ts, vol):
    return Candle(ts=ts, open=1, high=1, low=1, close=1, base_vol=vol, quote_vol=vol)


def perfil_plano(volumen=100.0, dias=14):
    velas = [vela(d * DIA + m * MINUTO, volumen) for d in range(dias) for m in range(1440)]
    cfg = ProfileConfig(history_days=14, smoothing_window_minutes=0,
                        min_days_for_confidence=3, rolling_fallback_candles=120)
    return build_profile("AAAUSDT", velas, cfg)


def perfil_con_baselines_distintos(dias=14):
    """Perfil donde el minuto 500 tiene baseline 50 y el minuto 501 tiene
    baseline 200; el resto de minutos llevan un valor de relleno irrelevante
    para el test. Se usa para distinguir sumar-y-dividir de promediar RVOLs."""
    def volumen_del_minuto(m):
        if m == 500:
            return 50.0
        if m == 501:
            return 200.0
        return 100.0

    velas = [vela(d * DIA + m * MINUTO, volumen_del_minuto(m))
             for d in range(dias) for m in range(1440)]
    cfg = ProfileConfig(history_days=14, smoothing_window_minutes=0,
                        min_days_for_confidence=3, rolling_fallback_candles=120)
    return build_profile("AAAUSDT", velas, cfg)


def perfil_con_slot_vacio(dias=14):
    """Perfil con muestras únicamente en el minuto 500; el minuto 501 no
    recibe ninguna vela y por tanto su slot queda sin baseline (None)."""
    velas = [vela(d * DIA + 500 * MINUTO, 100.0) for d in range(dias)]
    cfg = ProfileConfig(history_days=14, smoothing_window_minutes=0,
                        min_days_for_confidence=3, rolling_fallback_candles=120)
    return build_profile("AAAUSDT", velas, cfg)


def test_rvol_closed_es_el_cociente_simple():
    assert rvol_closed(850_000, 170_000) == 5.0


def test_rvol_closed_sin_baseline_devuelve_none():
    assert rvol_closed(850_000, None) is None


def test_rvol_closed_con_baseline_cero_devuelve_none():
    assert rvol_closed(850_000, 0) is None


def test_rvol_live_normaliza_por_la_fraccion_transcurrida():
    """A mitad de minuto, la mitad del volumen normal significa RVOL 1."""
    assert rvol_live(50, baseline=100, elapsed_seconds=30, min_elapsed_seconds=15) == 1.0


def test_rvol_live_detecta_explosion_a_mitad_de_minuto():
    assert rvol_live(300, baseline=100, elapsed_seconds=30, min_elapsed_seconds=15) == 6.0


def test_rvol_live_devuelve_none_antes_del_minimo_transcurrido():
    """En el segundo 2, un solo trade daría un RVOL absurdo."""
    assert rvol_live(50, baseline=100, elapsed_seconds=2, min_elapsed_seconds=15) is None


def test_rvol_live_al_completarse_el_minuto_coincide_con_closed():
    assert rvol_live(200, baseline=100, elapsed_seconds=60, min_elapsed_seconds=15) == 2.0


def test_rvol_live_acota_la_fraccion_a_uno():
    """Si por deriva de reloj llegan 61 segundos, no debe subestimar."""
    assert rvol_live(200, baseline=100, elapsed_seconds=75, min_elapsed_seconds=15) == 2.0


def test_rvol_live_en_el_limite_exacto_de_segundos_minimos_calcula_valor():
    """En elapsed_seconds == min_elapsed_seconds ya no debe devolver None:
    el corte es estrictamente '<', no '<='. Fija el valor exacto (no solo
    "no es None") para detectar un off-by-one en cualquier dirección."""
    resultado = rvol_live(50, baseline=100, elapsed_seconds=15, min_elapsed_seconds=15)
    assert resultado == 2.0


def test_rvol_window_agrega_volumen_y_baseline():
    perfil = perfil_plano(volumen=100.0)
    velas = [vela(500 * MINUTO + i * MINUTO, 300.0) for i in range(5)]
    assert rvol_window(velas, perfil) == 3.0


def test_rvol_window_suma_y_divide_en_vez_de_promediar_rvols():
    """Este test existe específicamente para distinguir "sumar volúmenes y
    sumar baselines, dividir una vez" (correcto) de "promediar el RVOL de
    cada vela individual" (incorrecto). Con un perfil plano ambas estrategias
    coinciden, por eso se usa aquí un perfil con baselines distintos por
    minuto: minuto 500 -> baseline 50, minuto 501 -> baseline 200.

    - suma-y-divide (correcto):      (500 + 200) / (50 + 200) = 2.8
    - promedio de RVOLs (incorrecto): (500/50 + 200/200) / 2  = 5.5

    Si alguien "simplifica" este test volviendo a un perfil plano, deja de
    detectar una regresión a la media de RVOLs individuales.
    """
    perfil = perfil_con_baselines_distintos()
    velas = [vela(500 * MINUTO, 500.0), vela(501 * MINUTO, 200.0)]
    resultado = rvol_window(velas, perfil)
    assert resultado == 2.8


def test_rvol_window_ignora_slots_sin_baseline():
    """El nombre pide un slot sin baseline: se construye un perfil con datos
    solo en el minuto 500, de modo que el minuto 501 quede sin ninguna
    muestra (baseline None). La vela del minuto 501 debe descartarse por
    completo -- ni su volumen ni un baseline se suman -- porque si solo se
    saltara el baseline pero se contara el volumen, el resultado quedaría
    inflado.

    - correcto (saltar la vela entera):        300 / 100        = 3.0
    - incorrecto (contar volumen, saltar base): (300 + 9999) / 100 = 102.99
    """
    perfil = perfil_con_slot_vacio()
    assert perfil.baseline(501) is None  # verifica la premisa del test
    velas = [vela(500 * MINUTO, 300.0), vela(501 * MINUTO, 9999.0)]
    resultado = rvol_window(velas, perfil)
    assert resultado == 3.0


def test_rvol_window_sin_velas_devuelve_none():
    assert rvol_window([], perfil_plano()) is None


def test_rvol_session_es_el_cociente_acumulado():
    assert rvol_session(3_900_000, 1_000_000) == 3.9


def test_rvol_session_sin_baseline_devuelve_none():
    assert rvol_session(3_900_000, None) is None
