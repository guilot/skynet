from scanner_volumen.config import ScoreConfig, ScoreCurve
from scanner_volumen.engine.metrics import SymbolMetrics
from scanner_volumen.models import Direction
from scanner_volumen.scoring.score import (
    ScoreBreakdown, detect_direction, piecewise, score_symbol,
)

CURVA_RVOL = ScoreCurve(max_points=15,
                        breakpoints=((1.0, 0.0), (3.0, 7.0), (5.0, 11.0), (10.0, 15.0)))
CURVA_MCAP = ScoreCurve(max_points=5,
                        breakpoints=((1_000_000_000.0, 0.0), (300_000_000.0, 2.0),
                                     (80_000_000.0, 5.0)))
CURVA_VWAP = ScoreCurve(max_points=8,
                        breakpoints=((-20.0, -8.0), (-2.0, 0.0), (2.0, 8.0),
                                     (6.0, 8.0), (10.0, 0.0), (15.0, -8.0)))


def test_piecewise_interpola_entre_puntos():
    assert piecewise(4.0, CURVA_RVOL) == 9.0  # a mitad entre 7 y 11


def test_piecewise_devuelve_el_valor_exacto_en_un_punto():
    assert piecewise(3.0, CURVA_RVOL) == 7.0


def test_piecewise_satura_por_encima_del_ultimo_punto():
    assert piecewise(50.0, CURVA_RVOL) == 15.0


def test_piecewise_satura_por_debajo_del_primer_punto():
    assert piecewise(0.2, CURVA_RVOL) == 0.0


def test_piecewise_con_none_devuelve_cero():
    assert piecewise(None, CURVA_RVOL) == 0.0


def test_piecewise_admite_breakpoints_descendentes():
    """Menos capitalización puntúa más."""
    assert piecewise(80_000_000, CURVA_MCAP) == 5.0
    assert piecewise(2_000_000_000, CURVA_MCAP) == 0.0
    assert piecewise(190_000_000, CURVA_MCAP) == 3.5


def test_piecewise_maneja_curvas_no_monotonas():
    assert piecewise(4.0, CURVA_VWAP) == 8.0     # zona óptima
    assert piecewise(10.0, CURVA_VWAP) == 0.0    # extendida
    assert piecewise(15.0, CURVA_VWAP) == -8.0   # muy extendida
    assert piecewise(30.0, CURVA_VWAP) == -8.0   # satura en el mínimo


def test_direccion_long_con_precio_sobre_vwap_y_momentum_positivo():
    assert detect_direction(2.8, 1.7) is Direction.LONG


def test_direccion_short_con_ambos_negativos():
    assert detect_direction(-2.8, -1.7) is Direction.SHORT


def test_direccion_neutral_cuando_discrepan():
    assert detect_direction(2.8, -1.7) is Direction.NEUTRAL
    assert detect_direction(-2.8, 1.7) is Direction.NEUTRAL


def test_direccion_neutral_sin_datos():
    assert detect_direction(None, 1.7) is Direction.NEUTRAL
    assert detect_direction(2.8, None) is Direction.NEUTRAL


# --- score completo ---

def cfg_completa():
    return ScoreConfig(
        neutral_multiplier=0.5,
        curves={
            "ret_1m": ScoreCurve(4, ((0.0, 0.0), (0.3, 2.0), (0.8, 4.0))),
            "ret_3m": ScoreCurve(6, ((0.0, 0.0), (0.8, 3.0), (2.0, 6.0))),
            "ret_5m": ScoreCurve(10, ((0.0, 0.0), (1.5, 5.0), (3.5, 10.0))),
            "ret_15m": ScoreCurve(8, ((0.0, 0.0), (2.5, 4.0), (5.0, 8.0))),
            "ret_1h": ScoreCurve(6, ((0.0, 0.0), (4.0, 3.0), (8.0, 6.0))),
            "ret_24h": ScoreCurve(6, ((0.0, 0.0), (5.0, 3.0), (12.0, 6.0))),
            "rvol_1m": CURVA_RVOL,
            "rvol_5m": ScoreCurve(10, ((1.0, 0.0), (2.0, 4.0), (3.0, 7.0), (6.0, 10.0))),
            "rvol_session": ScoreCurve(5, ((1.0, 0.0), (2.0, 3.0), (4.0, 5.0))),
            "demand_burst": ScoreCurve(10, ((1.0, 0.0), (1.5, 5.0), (2.5, 10.0))),
            "z_return": ScoreCurve(7, ((1.0, 0.0), (2.0, 3.0), (3.0, 5.0), (4.5, 7.0))),
            "market_cap": CURVA_MCAP,
            "vwap": CURVA_VWAP,
        },
    )


def metricas(**kwargs):
    base = dict(
        symbol="XYZUSDT", price=6.72,
        ret_1m=0.82, ret_3m=1.91, ret_5m=3.7, ret_15m=5.1,
        ret_30m=6.22, ret_1h=7.2, ret_24h=13.8,
        rvol_1m_closed=7.8, rvol_1m_live=8.1, rvol_5m=5.1, rvol_session=3.9,
        demand_burst=2.4, vwap=6.43, vwap_distance=4.5, z_return=3.2,
        market_cap=8e7, volume_24h=3.24e7, open_interest=1000.0,
        funding_rate=0.0001, profile_confidence="high", ts=0,
    )
    base.update(kwargs)
    return SymbolMetrics(**base)


def test_la_senal_de_ejemplo_del_concepto_puntua_alto():
    """Es el ejemplo de la §31 del documento de concepto."""
    r = score_symbol(metricas(), cfg_completa())
    assert r.direction is Direction.LONG
    assert r.total >= 80
    assert abs(r.momentum + r.demand + r.structure - r.raw_total) < 1e-9


def test_los_bloques_respetan_sus_topes():
    r = score_symbol(metricas(), cfg_completa())
    assert 0 <= r.momentum <= 40
    assert 0 <= r.demand <= 40
    assert r.structure <= 20


def test_una_moneda_plana_puntua_casi_cero():
    r = score_symbol(
        metricas(ret_1m=0.0, ret_3m=0.0, ret_5m=0.0, ret_15m=0.0, ret_30m=0.0,
                 ret_1h=0.0, ret_24h=0.0, rvol_1m_closed=1.0, rvol_1m_live=1.0,
                 rvol_5m=1.0, rvol_session=1.0, demand_burst=1.0,
                 vwap_distance=0.0, z_return=0.0, market_cap=2e9),
        cfg_completa(),
    )
    assert r.total < 10


def test_la_extension_excesiva_resta_puntos():
    normal = score_symbol(metricas(vwap_distance=4.0), cfg_completa())
    extendida = score_symbol(metricas(vwap_distance=18.0), cfg_completa())
    assert extendida.total < normal.total
    assert extendida.components["vwap"] == -8.0


def test_direccion_short_puntua_con_los_retornos_invertidos():
    r = score_symbol(
        metricas(ret_1m=-0.82, ret_3m=-1.91, ret_5m=-3.7, ret_15m=-5.1,
                 ret_30m=-6.22, ret_1h=-7.2, ret_24h=-13.8, vwap_distance=-4.5),
        cfg_completa(),
    )
    assert r.direction is Direction.SHORT
    assert r.momentum > 25
    # el mismo momentum que produciria el escenario LONG especular (todos los
    # retornos en signo contrario): confirma que se invierte el signo, no solo
    # se toma su magnitud por casualidad de que aqui todos son negativos.
    assert abs(r.momentum - 39.175) < 1e-9


def test_direccion_short_invierte_signo_no_solo_valor_absoluto():
    """Un retorno en contra de la tendencia SHORT debe penalizarse a cero, no
    premiarse como haria tomar el valor absoluto (que confundiria un +7.2%
    en contra con un -7.2% a favor)."""
    r = score_symbol(
        metricas(ret_1m=-0.82, ret_3m=-1.91, ret_5m=-3.7, ret_15m=-5.1,
                 ret_30m=-6.22, ret_1h=7.2, ret_24h=-13.8, vwap_distance=-4.5),
        cfg_completa(),
    )
    assert r.direction is Direction.SHORT
    assert r.components["ret_1h"] == 0.0


def test_direccion_neutral_aplica_el_multiplicador():
    cfg = cfg_completa()
    neutral = score_symbol(metricas(ret_5m=3.7, vwap_distance=-1.0), cfg)
    assert neutral.direction is Direction.NEUTRAL
    assert abs(neutral.total - neutral.raw_total * 0.5) < 1e-9


def test_el_total_se_acota_a_cien():
    r = score_symbol(
        metricas(ret_1m=99, ret_3m=99, ret_5m=99, ret_15m=99, ret_1h=99, ret_24h=99,
                 rvol_1m_closed=99, rvol_5m=99, rvol_session=99, demand_burst=99,
                 z_return=99, market_cap=1e6),
        cfg_completa(),
    )
    assert r.total == 100


def test_el_total_se_acota_a_cero_pero_raw_total_conserva_el_signo():
    r = score_symbol(
        metricas(ret_1m=0.0, ret_3m=0.0, ret_5m=0.01, ret_15m=0.0, ret_30m=0.0,
                 ret_1h=0.0, ret_24h=0.0, rvol_1m_closed=1.0, rvol_1m_live=1.0,
                 rvol_5m=1.0, rvol_session=1.0, demand_burst=1.0,
                 vwap_distance=30.0, z_return=0.0, market_cap=2e9),
        cfg_completa(),
    )
    assert r.total == 0
    assert r.raw_total < 0


def test_metricas_ausentes_no_lanzan_excepcion():
    r = score_symbol(
        metricas(ret_1m=None, ret_3m=None, ret_5m=None, ret_15m=None, ret_30m=None,
                 ret_1h=None, ret_24h=None, rvol_1m_closed=None, rvol_1m_live=None,
                 rvol_5m=None, rvol_session=None, demand_burst=None, vwap=None,
                 vwap_distance=None, z_return=None, market_cap=None),
        cfg_completa(),
    )
    assert r.total == 0
    assert r.direction is Direction.NEUTRAL
