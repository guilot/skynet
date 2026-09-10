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
    max_rank INTEGER,
    degradada INTEGER NOT NULL DEFAULT 0,
    client_oid TEXT,
    confirmada INTEGER NOT NULL DEFAULT 1,
    stop_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_bot_pos_abierta ON bot_posiciones(modo, abierta);
CREATE INDEX IF NOT EXISTS idx_bot_pos_client_oid ON bot_posiciones(modo, client_oid);

CREATE TABLE IF NOT EXISTS bot_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    posicion_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    reason TEXT NOT NULL,
    fraction REAL NOT NULL,
    precio_referencia REAL NOT NULL,
    precio REAL NOT NULL,
    comision REAL NOT NULL,
    tardio INTEGER NOT NULL DEFAULT 0,
    precio_regla REAL,
    cierre_exchange INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_bot_fills_pos ON bot_fills(posicion_id);

CREATE TABLE IF NOT EXISTS bot_meta (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);

-- Contadores del informe del bot (descartes, transiciones vistas, la
-- concurrencia máxima alcanzada): sin esto viven solo en RAM del proceso
-- (`LivePortfolio.descartes`, `BotRunner.transiciones_vistas`) y se
-- reinician con cada reinicio, así que el informe imprime "0" para
-- siempre -justo las líneas que explicarían por qué el paper tomó menos
-- trades que el backtest-. Clave compuesta `(modo, clave)` para que paper y
-- real no mezclen sus cuentas, igual que el resto de tablas del bot.
CREATE TABLE IF NOT EXISTS bot_contadores (
    modo TEXT NOT NULL,
    clave TEXT NOT NULL,
    valor INTEGER NOT NULL,
    PRIMARY KEY (modo, clave)
) WITHOUT ROWID;
"""


# Versión del esquema (I-4): sube cada vez que una migración en `_migrar`
# cambia columnas de una tabla que `CREATE TABLE IF NOT EXISTS` no puede
# tocar porque ya existe. `PRAGMA user_version` es el mecanismo nativo de
# SQLite para esto -entero simple embebido en el propio fichero, sin tabla
# adicional que crear ni de la que depender antes de tener esquema-.
VERSION_ESQUEMA = 8


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
    if version_actual < 4:
        _migrar_v4_bot_tablas(conn)
    if version_actual < 5:
        _migrar_v5_informe_bot(conn)
    if version_actual < 6:
        _migrar_v6_client_oid(conn)
    if version_actual < 7:
        _migrar_v7_stop_id(conn)
    if version_actual < 8:
        _migrar_v8_cierre_exchange(conn)
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


def _migrar_v4_bot_tablas(conn: sqlite3.Connection) -> None:
    """No-op declarado, igual que `_migrar_v3_state_transitions`:
    `bot_posiciones`, `bot_fills` y `bot_meta` se añadieron en su día como
    tablas enteramente nuevas -el propio `CREATE TABLE IF NOT EXISTS` de
    `ESQUEMA` ya las crea contra cualquier base existente sin tocar ninguna
    columna de una tabla ya existente-, pero esa entrega no subió
    `VERSION_ESQUEMA` para registrarlo. Este paso solo pone al día
    `PRAGMA user_version`, retroactivamente, con lo que el esquema actual ya
    incluye desde entonces."""
    return


def _migrar_v5_informe_bot(conn: sqlite3.Connection) -> None:
    """Añade a `bot_posiciones`/`bot_fills`, ya existentes, las columnas que
    necesita el informe de ejecución; `bot_contadores` es tabla enteramente
    nueva y no necesita ALTER (la crea el propio `CREATE TABLE IF NOT
    EXISTS` de `ESQUEMA`, igual que `_migrar_v4_bot_tablas`).

    `bot_posiciones.degradada` distingue una posición que el runner aisló
    tras un fallo del broker -sigue "abierta" en la base de datos, ocupando
    su hueco de concurrencia, pero ya nadie la gobierna- de una posición
    sana; sin esta columna esas posiciones desaparecían del informe sin
    dejar rastro, y son sistemáticamente las que iban perdiendo (el camino
    más probable a degradarse es un fallo al ejecutar un STOP).
    `bot_fills.precio_regla` guarda el nivel que la regla prometía para cada
    salida (el stop vigente, el break-even, o el precio de la transición,
    según el motivo), para medir el desvío de salida contra un nivel real en
    vez de contra el propio precio con el que el bot rellena su vela
    sintética -que siempre daría desvío cero, no porque la ejecución fuera
    perfecta, sino por construcción-.

    `ALTER TABLE ... ADD COLUMN` no exige `DEFAULT` para una columna
    nullable (`precio_regla`); `degradada` sí lleva `DEFAULT 0` porque nace
    NOT NULL -toda fila ya existente antes de esta migración se asume sana,
    que es lo correcto: una posición degradada solo pudo escribirse con
    código que ya conoce esta columna-.
    """
    columnas_pos = {f["name"] for f in conn.execute("PRAGMA table_info(bot_posiciones)")}
    if columnas_pos and "degradada" not in columnas_pos:
        conn.execute(
            "ALTER TABLE bot_posiciones ADD COLUMN degradada INTEGER NOT NULL DEFAULT 0"
        )
    columnas_fills = {f["name"] for f in conn.execute("PRAGMA table_info(bot_fills)")}
    if columnas_fills and "precio_regla" not in columnas_fills:
        conn.execute("ALTER TABLE bot_fills ADD COLUMN precio_regla REAL")
    conn.commit()


def _migrar_v6_client_oid(conn: sqlite3.Connection) -> None:
    """Añade a `bot_posiciones`, ya existente, la clave de idempotencia de la
    apertura: hoy se manda la orden al broker y DESPUÉS se escribe la fila; si
    el proceso muere entre ambas cosas, la posición queda abierta en el
    exchange sin ningún registro local, y la reconciliación (Task 8) la
    trataría como ajena. A partir de esta migración, `BotRunner._abrir` invierte
    el orden: genera un `client_oid`, reserva la fila con él ANTES de mandar la
    orden, y la confirma al recibir la respuesta.

    Lo natural sería reservar la fila con `entry_price` vacío y rellenarlo al
    confirmar. No se puede: esa columna es `NOT NULL` y SQLite no permite
    retirar esa restricción con un `ALTER TABLE` -habría que reconstruir la
    tabla entera, con el riesgo que eso tiene sobre una base de producción que
    ya lleva datos-. En su lugar, la fila se reserva con el precio de la señal
    como valor provisional en `entry_price` y `confirmada = 0`; al llegar la
    respuesta del broker se sobrescribe con el precio ejecutado (y el tamaño
    real) y se marca `confirmada = 1` (ver `BotRepo.confirmar_apertura`).

    `confirmada = 0` significa exactamente "orden mandada, resultado
    desconocido": es la huella que deja un proceso que murió entre mandar la
    orden y registrar su resultado, y es lo que Task 8 tiene que saber leer
    (ver `BotRepo.reservadas_sin_confirmar`). `ALTER TABLE ... ADD COLUMN ...
    NOT NULL` exige un `DEFAULT` en SQLite; se usa `1` porque toda fila ya
    existente antes de esta migración corresponde a una posición cuyo
    resultado ya se conocía al escribirla (el código anterior escribía la
    fila con el precio ejecutado en la mano), así que queda confirmada sin
    tocarla. `client_oid` es nullable: no hay ningún valor de relleno con
    sentido para una fila ya escrita antes de que este concepto existiera.

    El índice `(modo, client_oid)` vive en `ESQUEMA` (`idx_bot_pos_client_oid`),
    no aquí: `CREATE INDEX IF NOT EXISTS` no toca ninguna columna de una tabla
    ya existente, así que es seguro que lo cree siempre `open_db`, igual que
    el resto de índices del esquema."""
    columnas = {f["name"] for f in conn.execute("PRAGMA table_info(bot_posiciones)")}
    if columnas and "client_oid" not in columnas:
        conn.execute("ALTER TABLE bot_posiciones ADD COLUMN client_oid TEXT")
    if columnas and "confirmada" not in columnas:
        conn.execute(
            "ALTER TABLE bot_posiciones ADD COLUMN confirmada INTEGER NOT NULL DEFAULT 1"
        )
    conn.commit()


def _migrar_v7_stop_id(conn: sqlite3.Connection) -> None:
    """Añade a `bot_posiciones`, ya existente, el identificador del stop
    vigente en el exchange: hasta esta migración nadie lo colocaba, así que
    la Task 7 empieza a cablear el ciclo (colocar al abrir, mover a
    break-even, cancelar al cerrar) y necesita dónde persistirlo -sin esto,
    un reinicio del bot perdería el `stop_id` de toda posición que siguiera
    abierta y no podría cancelarlo ni moverlo nunca más.

    `ALTER TABLE ... ADD COLUMN` no exige `DEFAULT` para una columna
    nullable, y `stop_id` lo es: no hay ningún valor de relleno con sentido
    para una fila abierta antes de que este concepto existiera -esas
    posiciones, si siguen abiertas cuando se despliegue este código, se
    quedan sin stop en el exchange hasta que el bot las gobierne de nuevo,
    igual que ya les pasaba antes de esta tarea.

    Guarda de `PRAGMA table_info`, mismo patrón que `_migrar_v6_client_oid`:
    si la tabla no existe (base nueva) o si la columna ya está (un
    `open_db` repetido, o una base creada por el `CREATE TABLE IF NOT
    EXISTS` de `ESQUEMA`, que ya la incluye), no hay nada que hacer."""
    columnas = {f["name"] for f in conn.execute("PRAGMA table_info(bot_posiciones)")}
    if columnas and "stop_id" not in columnas:
        conn.execute("ALTER TABLE bot_posiciones ADD COLUMN stop_id TEXT")
    conn.commit()


def _migrar_v8_cierre_exchange(conn: sqlite3.Connection) -> None:
    """Añade a `bot_fills`, ya existente, la marca de que un cierre lo
    ejecutó el exchange por su cuenta -el stop saltó, o hubo liquidación-
    mientras el bot miraba a otro lado (Task 9, sondeo periódico).

    Deliberadamente NO se añade un valor nuevo a `ExitReason`: un cierre así
    sigue siendo, en espíritu, la regla del stop ejecutándose (motivo
    `STOP`), y `ExitReason` lo recorre entero el informe compartido con el
    backtest para imprimir el bloque de "PnL por motivo de salida" -un valor
    nuevo metería una línea nueva ahí y rompería el golden master del
    backtest sin que su comportamiento haya cambiado. Lo que distingue este
    cierre de un STOP local corriente es únicamente esta columna.

    `ALTER TABLE ... ADD COLUMN ... NOT NULL` exige un `DEFAULT` en SQLite;
    se usa `0` porque todo fill ya existente antes de esta migración lo
    ejecutó el propio bot, nunca un sondeo del exchange que todavía no
    existía.

    Guarda de `PRAGMA table_info`, mismo patrón que `_migrar_v7_stop_id`: si
    la tabla no existe (base nueva) o la columna ya está (un `open_db`
    repetido, o una base creada por el `CREATE TABLE IF NOT EXISTS` de
    `ESQUEMA`, que ya la incluye), no hay nada que hacer."""
    columnas = {f["name"] for f in conn.execute("PRAGMA table_info(bot_fills)")}
    if columnas and "cierre_exchange" not in columnas:
        conn.execute(
            "ALTER TABLE bot_fills ADD COLUMN cierre_exchange INTEGER NOT NULL DEFAULT 0"
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
