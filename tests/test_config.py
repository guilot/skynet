from pathlib import Path

import pytest

from scanner_volumen.config import CURVAS_ESPERADAS, load_config

# Anclado a la ubicación del propio fichero, no al cwd: sin esto la suite
# solo pasa si pytest se lanza desde la raíz del repo.
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


def test_carga_valores_del_toml():
    cfg = load_config(CONFIG_PATH)
    assert cfg.market.venue == "USDT-FUTURES"
    assert cfg.universe.min_volume_24h == 5_000_000
    assert cfg.universe.min_profile_median_volume == 2_000.0
    assert cfg.universe.max_symbols == 150
    assert cfg.engine.tick_seconds == 1.0
    assert cfg.states.signal == 80
    assert cfg.states.exit_ticks == 3

def test_curvas_de_score_se_cargan_como_dataclass():
    cfg = load_config(CONFIG_PATH)
    curva = cfg.score.curves["rvol_1m"]
    assert curva.max_points == 15
    assert curva.breakpoints[0] == (1.0, 0.0)
    assert curva.breakpoints[-1] == (10.0, 15.0)

def test_la_curva_vwap_admite_puntos_negativos():
    cfg = load_config(CONFIG_PATH)
    curva = cfg.score.curves["vwap"]
    assert curva.breakpoints[0] == (-20.0, -8.0)

def test_los_pesos_maximos_suman_cien():
    cfg = load_config(CONFIG_PATH)
    total = sum(c.max_points for c in cfg.score.curves.values())
    assert total == 100

def test_carga_la_cadencia_de_mantenimiento():
    # I2/I4: la cadencia de poda + recálculo diario de perfil es un umbral
    # de negocio y debe vivir en config.toml, no hardcodeada en __main__.
    cfg = load_config(CONFIG_PATH)
    assert cfg.maintenance.interval_hours == 24

def test_carga_el_umbral_de_obsolescencia_del_dashboard():
    # I1: el umbral que marca una fila del dashboard como obsoleta también
    # es un umbral de negocio configurable.
    cfg = load_config(CONFIG_PATH)
    assert cfg.dashboard.stale_after_seconds == 30

def test_carga_los_umbrales_de_burst_y_zscore():
    # I5: MUESTRAS_MINIMAS_Z, el lookback del demand burst y su denominador
    # mínimo (spec §6.3) ya no viven hardcodeados en engine/burst.py ni
    # engine/metrics.py.
    cfg = load_config(CONFIG_PATH)
    assert cfg.engine.zscore_min_samples == 8
    assert cfg.engine.burst_lookback_minutes == 5
    assert cfg.engine.demand_burst_min_denominator == 0.5

def test_carga_el_estado_minimo_de_alerta():
    cfg = load_config(CONFIG_PATH)
    assert cfg.states.alert_min_state == "SIGNAL"

def test_carga_la_configuracion_del_orquestador():
    # ESTADO_MINIMO_PERSISTIDO y MINUTOS_TOLERADOS_DE_HUECO, antes
    # hardcodeados en app/orchestrator.py.
    cfg = load_config(CONFIG_PATH)
    assert cfg.orchestrator.persisted_min_state == "HOT"
    assert cfg.orchestrator.gap_tolerance_minutes == 3

def test_carga_el_refresco_de_supply():
    # spec §4.3: supply_refresher cada 6 h.
    cfg = load_config(CONFIG_PATH)
    assert cfg.supply.refresh_hours == 6

def test_carga_los_horizontes_y_la_cadencia_de_outcomes():
    # spec §9 (horizontes 1,5,15,30,60) y spec §4.3 (outcome_tracker 1 min).
    cfg = load_config(CONFIG_PATH)
    assert cfg.outcomes.horizons_minutes == (1, 5, 15, 30, 60)
    assert cfg.outcomes.poll_seconds == 60

def test_carga_el_host_puerto_y_ruta_de_base_de_datos_del_servidor():
    cfg = load_config(CONFIG_PATH)
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8000
    assert cfg.server.db_path == "data/scanner.db"

def test_falla_si_alert_min_state_no_es_un_estado_valido(tmp_path):
    """Minor: antes, un typo en `alert_min_state` no fallaba hasta construir
    `StateMachine` (bien dentro del arranque de la app, `State(cfg.value)`
    lanzando `ValueError` sin contexto); debe fallar en `load_config`, un
    único punto de fallo al arrancar, igual que ya hacen las curvas."""
    toml_roto = CONFIG_PATH.read_text().replace(
        'alert_min_state = "SIGNAL"', 'alert_min_state = "SIGNL"',
    )
    destino = tmp_path / "config_roto.toml"
    destino.write_text(toml_roto)
    with pytest.raises(ValueError, match="alert_min_state"):
        load_config(destino)


def test_falla_si_persisted_min_state_no_es_un_estado_valido(tmp_path):
    toml_roto = CONFIG_PATH.read_text().replace(
        'persisted_min_state = "HOT"', 'persisted_min_state = "CALIENTE"',
    )
    destino = tmp_path / "config_roto.toml"
    destino.write_text(toml_roto)
    with pytest.raises(ValueError, match="persisted_min_state"):
        load_config(destino)


def test_curvas_esperadas_se_deriva_de_los_bloques_del_score_no_se_duplica():
    """Minor: `CURVAS_ESPERADAS` (13 nombres) y los tres bloques de
    `score_symbol` (MOMENTUM/DEMAND/STRUCTURE, en scoring/score.py) eran dos
    copias independientes de la misma lista de claves -- una curva nueva en
    un bloque de score.py sin añadirla aquí quedaría sin validar en el
    arranque. Debe derivarse de las mismas tuplas que usa score.py, no
    mantenerse como una lista aparte escrita a mano."""
    import scanner_volumen.config as config_mod
    from scanner_volumen.scoring.score import (
        CLAVES_DEMAND, CLAVES_MOMENTUM, CLAVES_STRUCTURE,
    )

    # identidad, no solo igualdad de valor: score.py debe importar las
    # mismas tuplas de config.py, no mantener su propia copia idéntica a
    # mano (eso pasaría igual esta aserción por casualidad de valor, sin
    # detectar la duplicación real).
    assert CLAVES_MOMENTUM is config_mod.CLAVES_MOMENTUM
    assert CLAVES_DEMAND is config_mod.CLAVES_DEMAND
    assert CLAVES_STRUCTURE is config_mod.CLAVES_STRUCTURE

    assert CURVAS_ESPERADAS == frozenset(
        (*CLAVES_MOMENTUM, *CLAVES_DEMAND, *CLAVES_STRUCTURE)
    )


def test_carga_los_umbrales_del_backtest():
    # Requisitos 2/3 del backtest: hueco de episodio, umbral mínimo de
    # episodios para significancia, y corte de cambio de scoring.
    cfg = load_config(CONFIG_PATH)
    assert cfg.backtest.episode_gap_minutes == 30
    assert cfg.backtest.min_episodes_for_significance == 30
    assert cfg.backtest.score_change_cutoff_ts == 1787004306142


def test_falla_si_falta_una_curva_de_score(tmp_path):
    """score_symbol filtra con `if clave in cfg.curves`: una curva ausente
    anularía en silencio hasta 15 puntos de un bloque entero sin que nada lo
    señalara. load_config debe fallar de inmediato, no dejar que la señal se
    pierda en producción."""
    toml_incompleto = CONFIG_PATH.read_text().replace(
        'demand_burst = { max_points = 10, breakpoints = [[1.0, 0], [1.5, 5], [2.5, 10]] }\n',
        "",
    )
    destino = tmp_path / "config_incompleto.toml"
    destino.write_text(toml_incompleto)
    with pytest.raises(ValueError, match="demand_burst"):
        load_config(destino)
