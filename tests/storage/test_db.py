# tests/storage/test_db.py
"""Regresión I-4: `signal_outcomes` ganó `candles_seen`/`candles_expected`
NOT NULL dentro de un `CREATE TABLE IF NOT EXISTS`, que es un no-op contra
cualquier base de datos ya existente. Sin una migración real, abrir una base
creada por el esquema viejo deja la tabla sin esas columnas, y todo
`save_outcome` posterior lanza `OperationalError` -que el bucle de outcomes
atrapa y registra una vez por minuto para siempre, sin que nadie note que
las salidas dejaron de grabarse-.
"""
import sqlite3

import pytest

from scanner_volumen.engine.metrics import SymbolMetrics
from scanner_volumen.models import Direction, State
from scanner_volumen.provenance import PRE_PROVENANCE_SENTINEL
from scanner_volumen.scoring.score import ScoreBreakdown
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import SignalRepo


def _metricas_de_prueba(**kwargs):
    base = dict(
        symbol="AAAUSDT", price=10.0, ret_1m=0.1, ret_3m=0.1, ret_5m=0.1,
        ret_15m=0.1, ret_30m=0.1, ret_1h=0.1, ret_24h=0.1,
        rvol_1m_closed=1.0, rvol_1m_live=1.0, rvol_5m=1.0, rvol_session=1.0,
        demand_burst=1.0, vwap=10.0, vwap_distance=0.0, z_return=1.0,
        market_cap=1e8, volume_24h=1e7, open_interest=1.0, funding_rate=0.0,
        profile_confidence="high", ts=2_000,
    )
    base.update(kwargs)
    return SymbolMetrics(**base)


def _desglose_de_prueba():
    return ScoreBreakdown(total=70.0, raw_total=70.0, momentum=30.0, demand=25.0,
                          structure=15.0, direction=Direction.LONG, components={})

# Esquema tal y como era antes de I-4: sin candles_seen/candles_expected.
# Solo las tablas relevantes para este test (signals + signal_outcomes);
# el resto de `open_db` no depende de que existan las demás para funcionar.
ESQUEMA_VIEJO = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    state TEXT NOT NULL,
    score REAL NOT NULL,
    score_momentum REAL NOT NULL,
    score_demand REAL NOT NULL,
    score_structure REAL NOT NULL,
    price REAL,
    rvol_1m_closed REAL,
    rvol_1m_live REAL,
    rvol_5m REAL,
    rvol_session REAL,
    demand_burst REAL,
    ret_1m REAL, ret_3m REAL, ret_5m REAL,
    ret_15m REAL, ret_30m REAL, ret_1h REAL, ret_24h REAL,
    vwap REAL,
    vwap_distance REAL,
    z_return REAL,
    market_cap REAL,
    volume_24h REAL,
    open_interest REAL,
    funding_rate REAL,
    profile_confidence TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_id INTEGER NOT NULL,
    horizon_min INTEGER NOT NULL,
    price REAL NOT NULL,
    return_pct REAL NOT NULL,
    mfe_pct REAL NOT NULL,
    mae_pct REAL NOT NULL,
    PRIMARY KEY (signal_id, horizon_min),
    FOREIGN KEY (signal_id) REFERENCES signals(id)
) WITHOUT ROWID;
"""


def _crear_db_con_esquema_viejo(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(ESQUEMA_VIEJO)
    conn.execute(
        """INSERT INTO signals
           (id, ts, symbol, direction, state, score, score_momentum,
            score_demand, score_structure, price, profile_confidence)
           VALUES (1, 1000, 'AAAUSDT', 'LONG', 'HOT', 70.0, 30.0, 25.0, 15.0,
                   100.0, 'high')""",
    )
    conn.commit()
    conn.close()


def test_abrir_una_base_del_esquema_viejo_no_rompe_save_outcome(tmp_path):
    """Regresión I-4: contra una base creada por el esquema viejo (sin
    candles_seen/candles_expected), `open_db` debe migrarla en vez de
    limitarse a un `CREATE TABLE IF NOT EXISTS` que es un no-op ahí."""
    path = tmp_path / "vieja.db"
    _crear_db_con_esquema_viejo(path)

    conn = open_db(path)
    try:
        columnas = {f["name"] for f in conn.execute("PRAGMA table_info(signal_outcomes)")}
        assert "candles_seen" in columnas
        assert "candles_expected" in columnas

        repo = SignalRepo(conn)
        # antes del fix, esto lanzaba sqlite3.OperationalError: la columna
        # no existía todavía en una base migrada por un CREATE TABLE
        # IF NOT EXISTS que era un no-op.
        repo.save_outcome(
            signal_id=1, horizon_min=5, price=101.0, return_pct=1.0,
            mfe_pct=1.5, mae_pct=-0.5, candles_seen=5, candles_expected=5,
        )
        filas = conn.execute("SELECT * FROM signal_outcomes").fetchall()
        assert len(filas) == 1
        assert filas[0]["candles_seen"] == 5
    finally:
        conn.close()


def test_abrir_una_base_del_esquema_viejo_no_rompe_filas_ya_existentes(tmp_path):
    """Las filas de `signal_outcomes` que ya existieran antes de migrar (en
    una base con historial real) deben sobrevivir con un valor por defecto
    en las columnas nuevas, no perderse ni romper la migración."""
    path = tmp_path / "vieja_con_datos.db"
    _crear_db_con_esquema_viejo(path)
    conn_previa = sqlite3.connect(path)
    conn_previa.execute(
        """INSERT INTO signal_outcomes
           (signal_id, horizon_min, price, return_pct, mfe_pct, mae_pct)
           VALUES (1, 1, 100.5, 0.5, 0.8, -0.2)"""
    )
    conn_previa.commit()
    conn_previa.close()

    conn = open_db(path)
    try:
        fila = conn.execute(
            "SELECT * FROM signal_outcomes WHERE signal_id = 1 AND horizon_min = 1"
        ).fetchone()
        assert fila is not None
        assert fila["price"] == pytest.approx(100.5)
        # valor por defecto explícito (-1) para lo que no se puede saber
        # retroactivamente, nunca NULL (violaría el propio NOT NULL) ni un
        # 0 que se confundiría con "ventana completa de cero velas".
        assert fila["candles_seen"] == -1
        assert fila["candles_expected"] == -1
    finally:
        conn.close()


def test_migra_signals_viejo_anade_columnas_de_procedencia_con_centinela(tmp_path):
    """Regresión de la tarea de procedencia: `signals` ganó
    `config_fingerprint`/`code_revision`, dos columnas NOT NULL, dentro de un
    `CREATE TABLE IF NOT EXISTS` que es un no-op contra una base ya
    existente -el mismo problema que motivó I-4, ahora contra la base de
    producción real con 285 filas (ver config.toml/config.dev.toml y el
    informe de la tarea): sin una migración real, abrir esa base con el
    código actual dejaría la tabla `signals` sin esas columnas, y cualquier
    INSERT posterior (SignalRepo.insert) rompería en tiempo de ejecución."""
    path = tmp_path / "vieja_sin_procedencia.db"
    _crear_db_con_esquema_viejo(path)  # crea también la fila id=1

    conn = open_db(path)
    try:
        columnas = {f["name"] for f in conn.execute("PRAGMA table_info(signals)")}
        assert "config_fingerprint" in columnas
        assert "code_revision" in columnas

        # la fila migrada (procedencia desconocida, grabada antes de que
        # existiera esta columna) lleva el centinela, no NULL ni "" ni un
        # valor que pudiera confundirse con un fingerprint real.
        fila_migrada = conn.execute(
            "SELECT config_fingerprint, code_revision FROM signals WHERE id = 1"
        ).fetchone()
        assert fila_migrada["config_fingerprint"] == PRE_PROVENANCE_SENTINEL
        assert fila_migrada["code_revision"] == PRE_PROVENANCE_SENTINEL

        # y la base migrada sigue siendo utilizable: una señal nueva, grabada
        # por el código actual, SÍ lleva procedencia real y es distinguible
        # de la migrada.
        repo = SignalRepo(conn)
        sid = repo.insert(
            _metricas_de_prueba(), _desglose_de_prueba(), State.SIGNAL,
            config_fingerprint="f" * 64, code_revision="abc1234",
        )
        fila_nueva = conn.execute(
            "SELECT config_fingerprint, code_revision FROM signals WHERE id = ?", (sid,)
        ).fetchone()
        assert fila_nueva["config_fingerprint"] == "f" * 64
        assert fila_nueva["code_revision"] == "abc1234"
    finally:
        conn.close()


def test_abrir_dos_veces_la_misma_base_es_idempotente(tmp_path):
    """`open_db` puede llamarse más de una vez sobre la misma base (p. ej.
    tests que reabren, o un proceso reiniciado): la migración no debe
    fallar la segunda vez por intentar añadir una columna que ya existe."""
    path = tmp_path / "reabierta.db"
    conn1 = open_db(path)
    conn1.close()

    conn2 = open_db(path)  # no debe lanzar
    try:
        columnas = {f["name"] for f in conn2.execute("PRAGMA table_info(signal_outcomes)")}
        assert "candles_seen" in columnas
    finally:
        conn2.close()
