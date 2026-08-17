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


def open_db(path: Path) -> sqlite3.Connection:
    """Abre (creando si hace falta) la base de datos y aplica el esquema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(ESQUEMA)
    conn.commit()
    return conn
