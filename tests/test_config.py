from pathlib import Path
from scanner_volumen.config import load_config

def test_carga_valores_del_toml():
    cfg = load_config(Path("config.toml"))
    assert cfg.market.venue == "USDT-FUTURES"
    assert cfg.universe.min_volume_24h == 1_000_000
    assert cfg.universe.max_symbols == 150
    assert cfg.engine.tick_seconds == 1.0
    assert cfg.states.signal == 80
    assert cfg.states.exit_ticks == 3

def test_curvas_de_score_se_cargan_como_dataclass():
    cfg = load_config(Path("config.toml"))
    curva = cfg.score.curves["rvol_1m"]
    assert curva.max_points == 15
    assert curva.breakpoints[0] == (1.0, 0.0)
    assert curva.breakpoints[-1] == (10.0, 15.0)

def test_la_curva_vwap_admite_puntos_negativos():
    cfg = load_config(Path("config.toml"))
    curva = cfg.score.curves["vwap"]
    assert curva.breakpoints[0] == (-20.0, -8.0)

def test_los_pesos_maximos_suman_cien():
    cfg = load_config(Path("config.toml"))
    total = sum(c.max_points for c in cfg.score.curves.values())
    assert total == 100
