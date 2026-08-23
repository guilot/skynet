from pathlib import Path

from scanner_volumen.backtest.__main__ import main
from scanner_volumen.engine.metrics import SymbolMetrics
from scanner_volumen.models import Direction, State
from scanner_volumen.scoring.score import ScoreBreakdown
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import SignalRepo

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.toml"


def _metricas(ts):
    return SymbolMetrics(
        symbol="AAAUSDT", price=10.0, ret_1m=0.1, ret_3m=0.1, ret_5m=0.1,
        ret_15m=0.1, ret_30m=0.1, ret_1h=0.1, ret_24h=0.1,
        rvol_1m_closed=1.0, rvol_1m_live=1.0, rvol_5m=1.0, rvol_session=1.0,
        demand_burst=1.0, vwap=10.0, vwap_distance=0.0, z_return=1.0,
        market_cap=1e8, volume_24h=1e7, open_interest=1.0, funding_rate=0.0,
        profile_confidence="high", ts=ts,
    )


def _construir_base_sintetica(ruta):
    conn = open_db(ruta)
    repo = SignalRepo(conn)
    sid = repo.insert(
        _metricas(0),
        ScoreBreakdown(total=85.0, raw_total=85.0, momentum=35.0, demand=35.0,
                       structure=15.0, direction=Direction.LONG, components={}),
        State.HOT,
        config_fingerprint="b" * 64, code_revision="test-rev",
    )
    repo.save_outcome(sid, 5, price=10.2, return_pct=2.0, mfe_pct=2.0, mae_pct=0.0,
                      candles_seen=5, candles_expected=5)
    conn.close()


def test_main_imprime_el_informe_sin_lanzar(tmp_path, capsys):
    ruta_db = tmp_path / "scanner.db"
    _construir_base_sintetica(ruta_db)

    main(["--db", str(ruta_db), "--config", str(CONFIG_PATH)])

    salida = capsys.readouterr().out
    assert "BACKTEST DE SEÑALES" in salida
    assert "AVISO METODOLÓGICO" in salida
    assert "MUESTRA INSUFICIENTE" in salida  # 1 episodio, muy por debajo del umbral


def test_main_no_lanza_con_una_consola_de_codepage_limitado(tmp_path, monkeypatch):
    """Regresión: en Windows, `sys.stdout` sin reconfigurar hereda el
    codepage de la consola (p. ej. cp437/cp850 en consolas heredadas), que
    no puede representar los acentos del informe en español y lanzaría
    `UnicodeEncodeError` a mitad de la tabla si `main` no fuerza UTF-8."""
    import io

    ruta_db = tmp_path / "scanner.db"
    _construir_base_sintetica(ruta_db)

    consola_limitada = io.TextIOWrapper(
        io.BytesIO(), encoding="cp437", errors="strict", write_through=True
    )
    monkeypatch.setattr("sys.stdout", consola_limitada)

    main(["--db", str(ruta_db), "--config", str(CONFIG_PATH)])  # no debe lanzar

    consola_limitada.flush()
    consola_limitada.buffer.seek(0)
    salida = consola_limitada.buffer.read().decode("utf-8")
    assert "BACKTEST DE SE" in salida


def test_main_no_modifica_la_base_de_datos(tmp_path, capsys):
    """El scanner puede seguir escribiendo en la base mientras corre el
    backtest: debe abrirla en solo lectura y no dejar ningún efecto."""
    import sqlite3

    ruta_db = tmp_path / "scanner.db"
    _construir_base_sintetica(ruta_db)
    tamaño_antes = ruta_db.stat().st_size
    contenido_antes = ruta_db.read_bytes()

    main(["--db", str(ruta_db), "--config", str(CONFIG_PATH)])

    assert ruta_db.stat().st_size == tamaño_antes
    assert ruta_db.read_bytes() == contenido_antes
