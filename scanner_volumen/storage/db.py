# scanner_volumen/storage/db.py
"""Esquema y apertura de la base de datos SQLite."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from scanner_volumen.provenance import PRE_PROVENANCE_SENTINEL

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
    profile_confidence TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    code_revision TEXT NOT NULL
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

-- Tabla de una sola fila (el CHECK fija `id` a 1): registra en qué ts del
-- reloj del exchange completó trabajo real la última vez `Orchestrator.
-- run_maintenance` (poda I2 + recálculo de perfil I4). Ver
-- `MaintenanceRepo` (storage/repos.py) y `paso_mantenimiento` (__main__.py)
-- para por qué esto vive en su propia tabla y no se deriva de
-- `MAX(profile_meta.updated_ms)`: esa columna también la escribe
-- `Bootstrapper.bootstrap_symbol` en cada alta de universo normal, no solo
-- el mantenimiento diario, así que su máximo no distinguiría "se
-- bootstrapeó un símbolo nuevo" de "corrió el ciclo de mantenimiento".
-- Tabla nueva: un `CREATE TABLE IF NOT EXISTS` no toca ninguna tabla ya
-- existente, así que no hace falta ninguna migración explícita para que
-- esto sea seguro contra la base de producción.
CREATE TABLE IF NOT EXISTS maintenance_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_completed_ms INTEGER NOT NULL
);

-- Trayectoria completa de estados por símbolo (WATCH o superior), incluidas
-- las transiciones que "mueren" (p. ej. WATCH -> NORMAL) que `signals` nunca
-- captura porque solo persiste escaladas a HOT+ (ver `Orchestrator.
-- evaluate`). Solo logging: a diferencia de `signals`, esta tabla no tiene
-- ningún `..._outcomes` asociado -el análisis posterior calcula los retornos
-- cruzando estos `ts` con `candles_1m`, no desde aquí-. Ver
-- `StateTransitionRepo` (storage/repos.py) y el informe de la tarea.
-- Tabla nueva, igual que `maintenance_meta`: un `CREATE TABLE IF NOT
-- EXISTS` no toca ninguna tabla ya existente, así que no hace falta ninguna
-- migración de columnas para que esto sea seguro contra la base de
-- producción -solo se sube `VERSION_ESQUEMA` para que quede registrado que
-- el esquema actual la incluye (ver `_migrar_v3_state_transitions`).
CREATE TABLE IF NOT EXISTS state_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    prev_state TEXT NOT NULL,
    new_state TEXT NOT NULL,
    score REAL NOT NULL,
    price REAL,
    direction TEXT NOT NULL,
    escalated INTEGER NOT NULL,
    config_fingerprint TEXT NOT NULL,
    code_revision TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_state_transitions_ts ON state_transitions(ts);

CREATE TABLE IF NOT EXISTS bot_posiciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    modo TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_ts INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    entry_price_senal REAL NOT NULL,
    margin REAL NOT NULL,
    notional REAL NOT NULL,
    size REAL NOT NULL,
    fee_entrada REAL NOT NULL,
    abierta INTEGER NOT NULL,
    close_ts INTEGER,
    pnl REAL,
    fees REAL,
    max_rank INTEGER
);

CREATE INDEX IF NOT EXISTS idx_bot_pos_abierta ON bot_posiciones(modo, abierta);

CREATE TABLE IF NOT EXISTS bot_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    posicion_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    reason TEXT NOT NULL,
    fraction REAL NOT NULL,
    precio_referencia REAL NOT NULL,
    precio REAL NOT NULL,
    comision REAL NOT NULL,
    tardio INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_bot_fills_pos ON bot_fills(posicion_id);

CREATE TABLE IF NOT EXISTS bot_meta (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
"""


# Versión del esquema (I-4): sube cada vez que una migración en `_migrar`
# cambia columnas de una tabla que `CREATE TABLE IF NOT EXISTS` no puede
# tocar porque ya existe. `PRAGMA user_version` es el mecanismo nativo de
# SQLite para esto -entero simple embebido en el propio fichero, sin tabla
# adicional que crear ni de la que depender antes de tener esquema-.
VERSION_ESQUEMA = 3


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
    if version_actual < 2:
        _migrar_v2_procedencia_de_signals(conn)
    if version_actual < 3:
        _migrar_v3_state_transitions(conn)
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


def _migrar_v2_procedencia_de_signals(conn: sqlite3.Connection) -> None:
    """Añade `config_fingerprint`/`code_revision` a un `signals` viejo.

    Este es el caso real que motivó la tarea de procedencia: la base de
    producción tiene 285 filas ya grabadas (227 de corridas de desarrollo
    mezcladas con 58 de producción, distinguibles solo por fecha) sin forma
    de reconstruir qué config o qué revisión de código las produjo -eso es
    justamente lo que estas columnas empiezan a registrar de aquí en
    adelante-. `ALTER TABLE ... ADD COLUMN ... NOT NULL` exige un `DEFAULT`
    literal en SQLite (no puede dejar NULL las filas ya existentes); se usa
    `PRE_PROVENANCE_SENTINEL` (`provenance.py`) -un string que nunca puede
    confundirse con un fingerprint sha256 real (64 hex) ni con una revisión
    git real- para que una fila migrada sea inconfundible con una calculada
    por el código actual, mismo patrón que el `-1` de
    `_migrar_v1_columnas_de_outcome`.

    Si `signals` todavía no existe (base nueva), no hay nada que migrar: el
    `CREATE TABLE IF NOT EXISTS` de `ESQUEMA`, que corre después, la crea ya
    completa con las columnas nuevas incluidas.
    """
    columnas = {f["name"] for f in conn.execute("PRAGMA table_info(signals)")}
    if not columnas:
        return
    if "config_fingerprint" not in columnas:
        conn.execute(
            "ALTER TABLE signals ADD COLUMN config_fingerprint TEXT NOT NULL "
            f"DEFAULT '{PRE_PROVENANCE_SENTINEL}'"
        )
    if "code_revision" not in columnas:
        conn.execute(
            "ALTER TABLE signals ADD COLUMN code_revision TEXT NOT NULL "
            f"DEFAULT '{PRE_PROVENANCE_SENTINEL}'"
        )
    conn.commit()


def _migrar_v3_state_transitions(conn: sqlite3.Connection) -> None:
    """No-op declarado: `state_transitions` es una tabla enteramente nueva
    (igual que `maintenance_meta`), así que el propio `CREATE TABLE IF NOT
    EXISTS` de `ESQUEMA` -que corre después de `_migrar`, sin condicionar a
    la versión- ya la crea contra cualquier base existente sin tocar ninguna
    columna de una tabla ya existente. Este paso solo existe para que
    `VERSION_ESQUEMA`/`PRAGMA user_version` reflejen que el esquema actual
    incluye `state_transitions`, igual que el resto de pasos numerados de
    `_migrar`."""
    return


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


def open_readonly(path: Path) -> sqlite3.Connection:
    """Apertura de la base de datos en modo solo-lectura.

    El scanner puede seguir corriendo y escribiendo en la misma base mientras se
    ejecuta un backtest (spec: "Do not modify that database"). `open_db` no
    sirve aquí: aplica migraciones y `executescript(ESQUEMA)`, escrituras que
    esta herramienta no necesita y que no debe arriesgarse a hacer contra una
    base en uso. Se abre con el URI `mode=ro` de SQLite, que hace que
    cualquier intento de escritura falle en el propio driver -no solo "no se
    escribe por convención", sino que no puede escribirse-.
    """
    if not path.exists():
        raise FileNotFoundError(f"no existe la base de datos: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn
