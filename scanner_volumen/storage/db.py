# scanner_volumen/storage/db.py
"""Esquema y apertura de la base de datos SQLite."""
from __future__ import annotations

import sqlite3
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS candles_1m (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    base_vol REAL NOT NULL,
    quote_vol REAL NOT NULL,
    PRIMARY KEY (symbol, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_candles_ts ON candles_1m(ts);

CREATE TABLE IF NOT EXISTS volume_profile (
    symbol TEXT NOT NULL,
    minute_of_day INTEGER NOT NULL,
    median REAL NOT NULL,
    p75 REAL NOT NULL,
    p90 REAL NOT NULL,
    p95 REAL NOT NULL,
    samples INTEGER NOT NULL,
    PRIMARY KEY (symbol, minute_of_day)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS profile_meta (
    symbol TEXT PRIMARY KEY,
    confidence TEXT NOT NULL,
    days_covered REAL NOT NULL,
    updated_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS supply_cache (
    symbol TEXT PRIMARY KEY,
    coingecko_id TEXT,
    circulating_supply REAL,
    market_cap REAL,
    fdv REAL,
    updated_ms INTEGER NOT NULL
);

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

CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);

CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_id INTEGER NOT NULL,
    horizon_min INTEGER NOT NULL,
    price REAL NOT NULL,
    return_pct REAL NOT NULL,
    mfe_pct REAL NOT NULL,
    mae_pct REAL NOT NULL,
    candles_seen INTEGER NOT NULL,
    candles_expected INTEGER NOT NULL,
    PRIMARY KEY (signal_id, horizon_min),
    FOREIGN KEY (signal_id) REFERENCES signals(id)
) WITHOUT ROWID;
"""


# Versión del esquema (I-4): sube cada vez que una migración en `_migrar`
# cambia columnas de una tabla que `CREATE TABLE IF NOT EXISTS` no puede
# tocar porque ya existe. `PRAGMA user_version` es el mecanismo nativo de
# SQLite para esto -entero simple embebido en el propio fichero, sin tabla
# adicional que crear ni de la que depender antes de tener esquema-.
VERSION_ESQUEMA = 1


def _migrar(conn: sqlite3.Connection) -> None:
    """Aplica las migraciones pendientes contra una base ya existente.

    `CREATE TABLE IF NOT EXISTS` (ver `ESQUEMA`) es un no-op contra una
    tabla que ya existe: nunca añade una columna nueva. Sin esto, abrir con
    el código actual una base creada por una versión anterior del esquema
    deja la tabla vieja tal cual, y cualquier INSERT/SELECT que dependa de
    una columna añadida después rompe en tiempo de ejecución -el caso real
    que motivó esto: `signal_outcomes` ganó `candles_seen`/`candles_expected`
    NOT NULL, y sin migración cada `save_outcome` sobre una base vieja
    lanzaba `OperationalError`, que el bucle de outcomes atrapa y registra
    una vez por minuto para siempre en vez de propagarse-.

    Cada paso de migración se guarda por separado (no solo el número de
    versión final) y es idempotente por sí mismo -comprueba las columnas
    reales con `PRAGMA table_info` antes de tocar nada-, así que abrir dos
    veces la misma base, o una base que ya tenga `user_version` al día, no
    falla ni duplica trabajo.
    """
    version_actual = conn.execute("PRAGMA user_version").fetchone()[0]
    if version_actual < 1:
        _migrar_v1_columnas_de_outcome(conn)
    if version_actual < VERSION_ESQUEMA:
        conn.execute(f"PRAGMA user_version = {VERSION_ESQUEMA}")
        conn.commit()


def _migrar_v1_columnas_de_outcome(conn: sqlite3.Connection) -> None:
    """Añade `candles_seen`/`candles_expected` a un `signal_outcomes` viejo.

    `ALTER TABLE ... ADD COLUMN ... NOT NULL` exige un `DEFAULT` en SQLite
    (no puede dejar NULL las filas ya existentes). Se usa `-1`, un valor que
    ninguna cuenta real de velas puede tomar, para que una fila migrada sea
    distinguible de una calculada por el código actual (que siempre escribe
    un conteo real, ver `OutcomeTracker.run_once`) en vez de confundirse con
    "ventana de cero velas".

    Si la tabla `signal_outcomes` todavía no existe (base nueva, o base
    vieja que ni siquiera llegó a crear la tabla), no hay nada que migrar:
    el `CREATE TABLE IF NOT EXISTS` de `ESQUEMA`, que corre después, la crea
    ya completa con las columnas nuevas incluidas.
    """
    columnas = {f["name"] for f in conn.execute("PRAGMA table_info(signal_outcomes)")}
    if not columnas:
        return
    if "candles_seen" not in columnas:
        conn.execute(
            "ALTER TABLE signal_outcomes ADD COLUMN candles_seen INTEGER NOT NULL DEFAULT -1"
        )
    if "candles_expected" not in columnas:
        conn.execute(
            "ALTER TABLE signal_outcomes ADD COLUMN candles_expected INTEGER NOT NULL DEFAULT -1"
        )
    conn.commit()


def open_db(path: Path) -> sqlite3.Connection:
    """Abre (creando si hace falta) la base de datos, migra el esquema de
    una base ya existente si hace falta, y aplica el esquema actual."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrar(conn)
    conn.executescript(ESQUEMA)
    conn.commit()
    return conn
