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


def test_rvol_window_agrega_volumen_y_baseline():
    perfil = perfil_plano(volumen=100.0)
    velas = [vela(500 * MINUTO + i * MINUTO, 300.0) for i in range(5)]
    assert rvol_window(velas, perfil) == 3.0


def test_rvol_window_ignora_slots_sin_baseline():
    perfil = perfil_plano(volumen=100.0)
    velas = [vela(500 * MINUTO + i * MINUTO, 300.0) for i in range(5)]
    resultado = rvol_window(velas, perfil)
    assert resultado is not None and resultado > 0


def test_rvol_window_sin_velas_devuelve_none():
    assert rvol_window([], perfil_plano()) is None


def test_rvol_session_es_el_cociente_acumulado():
    assert rvol_session(3_900_000, 1_000_000) == 3.9


def test_rvol_session_sin_baseline_devuelve_none():
    assert rvol_session(3_900_000, None) is None
