import tomllib
from pathlib import Path

import pytest

from scanner_volumen.bot.modo import resolver_modo
from scanner_volumen.config import CURVAS_ESPERADAS, load_config

# Anclado a la ubicación del propio fichero, no al cwd: sin esto la suite
# solo pasa si pytest se lanza desde la raíz del repo.
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"
CONFIG_DEV_PATH = CONFIG_PATH.parent / "config.dev.toml"


def _diferencias_planas(a: dict, b: dict, prefijo: str = "") -> dict[str, tuple]:
    """Compara dos TOML ya parseados (posiblemente anidados, p.ej.
    `[score.curves]`) y devuelve, en formato plano `"seccion.clave"`, solo
    las entradas cuyo valor difiere. Falla de inmediato si un dict tiene
    claves que el otro no tiene -dev y producción deben tener exactamente
    las mismas secciones y campos, nunca uno de más o de menos."""
    claves_a, claves_b = set(a), set(b)
    assert claves_a == claves_b, (
        f"claves distintas en {prefijo or '<raíz>'!r}: {claves_a ^ claves_b}"
    )
    diffs: dict[str, tuple] = {}
    for clave in claves_a:
        valor_a, valor_b = a[clave], b[clave]
        ruta = f"{prefijo}{clave}"
        if isinstance(valor_a, dict) and isinstance(valor_b, dict):
            diffs.update(_diferencias_planas(valor_a, valor_b, prefijo=f"{ruta}."))
        elif valor_a != valor_b:
            diffs[ruta] = (valor_a, valor_b)
    return diffs


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
    # 1787070798483 = ts de la señal id 44, la primera puntuada con la curva
    # de VWAP endurecida (commit e1937d8); ver el comentario junto al valor
    # en config.toml para la evidencia completa. El valor antiguo
    # (1787004306142, ts de la señal id 43) era el ts de la señal más
    # reciente en el momento en que se miró la base de datos -no el momento
    # en que el cambio de curva entró en vigor- y quedaba 17 h por delante
    # del corte real.
    assert cfg.backtest.score_change_cutoff_ts == 1787070798483


def test_falla_si_episode_gap_minutes_no_es_positivo(tmp_path):
    """Minor: sin esto, `episode_gap_minutes = 0` no fallaba hasta
    `episodes.group_episodes`, en tiempo de ejecución del backtest y sin
    contexto sobre qué campo del TOML lo causó."""
    toml_roto = CONFIG_PATH.read_text().replace(
        "episode_gap_minutes = 30", "episode_gap_minutes = 0",
    )
    destino = tmp_path / "config_roto.toml"
    destino.write_text(toml_roto)
    with pytest.raises(ValueError, match="episode_gap_minutes"):
        load_config(destino)


def test_config_bot_se_carga_entera():
    """La sección [bot] carga con los cuatro campos bien tipados.

    `enabled` NO se afirma por valor: si el bot está encendido o apagado es una
    decisión de despliegue que vive en `config.toml`, no un invariante del
    código. Fijarlo aquí obligaría a tocar este test cada vez que se enciende o
    se apaga el bot, que es justo lo que no debe costar nada."""
    cfg = load_config(CONFIG_PATH)
    assert isinstance(cfg.bot.enabled, bool)
    assert cfg.bot.modo == "paper"
    assert cfg.bot.equity_inicial == 1000.0
    assert cfg.bot.desvio_max_entrada == 0.0  # 0 = desactivado


def test_modo_real_se_acepta_en_la_config_pero_no_basta(tmp_path):
    """`load_config` ya no rechaza `real`: el rechazo vive ahora en
    `resolver_modo`, que exige además la variable de entorno. Este test fija
    que la config SOLA nunca es suficiente para llegar a dinero real."""
    origen = CONFIG_PATH.read_text(encoding="utf-8")
    destino = tmp_path / "config.toml"
    destino.write_text(origen.replace('modo = "paper"', 'modo = "real"'),
                       encoding="utf-8")
    cfg = load_config(destino)          # ya no lanza
    assert cfg.bot.modo == "real"
    with pytest.raises(ValueError, match="SCANNER_BOT_REAL"):
        resolver_modo(cfg.bot, {})      # pero sola no basta


def test_modo_desconocido_falla(tmp_path):
    origen = CONFIG_PATH.read_text(encoding="utf-8")
    destino = tmp_path / "config.toml"
    destino.write_text(origen.replace('modo = "paper"', 'modo = "simulado"'),
                       encoding="utf-8")
    with pytest.raises(ValueError, match="bot.modo"):
        load_config(destino)


def test_falla_si_min_episodes_for_significance_es_negativo(tmp_path):
    toml_roto = CONFIG_PATH.read_text().replace(
        "min_episodes_for_significance = 30", "min_episodes_for_significance = -1",
    )
    destino = tmp_path / "config_roto.toml"
    destino.write_text(toml_roto)
    with pytest.raises(ValueError, match="min_episodes_for_significance"):
        load_config(destino)


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


def test_config_dev_solo_difiere_de_produccion_en_db_path_y_puerto():
    """Guarda la invariante que hace que dev *represente* producción: la
    contaminación real que motivó esta separación de entornos (285 señales
    en la base de producción, 227 de ellas de corridas de desarrollo
    copiadas encima) fue posible porque no había ninguna frontera física
    entre ambos entornos. `config.dev.toml` debe ser idéntico a
    `config.toml` salvo `server.db_path` y `server.port` -si alguien cambia
    un umbral de negocio (universo, curvas de score, cadencias...) en un
    solo fichero, dev deja de representar producción y este test debe
    fallar antes de que ese drift llegue a esconder un bug."""
    with CONFIG_PATH.open("rb") as fh:
        produccion = tomllib.load(fh)
    with CONFIG_DEV_PATH.open("rb") as fh:
        dev = tomllib.load(fh)

    diferencias = _diferencias_planas(produccion, dev)

    assert diferencias == {
        "server.db_path": ("data/scanner.db", "data/dev.db"),
        "server.port": (8000, 8001),
    }
