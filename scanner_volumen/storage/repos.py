# scanner_volumen/storage/repos.py
"""Acceso a datos. Aísla el resto del sistema del motor de base de datos:
migrar a PostgreSQL en V3 debería tocar solo este fichero.
"""
from __future__ import annotations

import sqlite3

from scanner_volumen.engine.metrics import SymbolMetrics
from scanner_volumen.engine.profile import MINUTOS_POR_DIA, SlotStats, VolumeProfile
from scanner_volumen.models import Candle, Direction, State
from scanner_volumen.scoring.score import ScoreBreakdown
from scanner_volumen.scoring.states import Transition


class CandleRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save_many(self, symbol: str, candles: list[Candle]) -> None:
        """Inserta velas nuevas; una vela para el mismo (symbol, ts) que ya
        existe se sobrescribe en vez de duplicarse o fallar, porque la vela
        del minuto en curso llega repetidamente mientras se va formando."""
        self._conn.executemany(
            """INSERT INTO candles_1m
               (symbol, ts, open, high, low, close, base_vol, quote_vol)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol, ts) DO UPDATE SET
                 open=excluded.open, high=excluded.high, low=excluded.low,
                 close=excluded.close, base_vol=excluded.base_vol,
                 quote_vol=excluded.quote_vol""",
            [
                (symbol, c.ts, c.open, c.high, c.low, c.close, c.base_vol, c.quote_vol)
                for c in candles
            ],
        )
        self._conn.commit()

    def load(self, symbol: str, since_ms: int) -> list[Candle]:
        filas = self._conn.execute(
            """SELECT ts, open, high, low, close, base_vol, quote_vol
               FROM candles_1m WHERE symbol = ? AND ts >= ? ORDER BY ts""",
            (symbol, since_ms),
        ).fetchall()
        return [
            Candle(ts=f["ts"], open=f["open"], high=f["high"], low=f["low"],
                   close=f["close"], base_vol=f["base_vol"], quote_vol=f["quote_vol"])
            for f in filas
        ]

    def latest_ts(self, symbol: str) -> int | None:
        fila = self._conn.execute(
            "SELECT MAX(ts) AS m FROM candles_1m WHERE symbol = ?", (symbol,)
        ).fetchone()
        return fila["m"] if fila and fila["m"] is not None else None

    def prune(self, older_than_ms: int) -> None:
        self._conn.execute("DELETE FROM candles_1m WHERE ts < ?", (older_than_ms,))
        self._conn.commit()


class ProfileRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, profile: VolumeProfile, now_ms: int) -> None:
        """Guarda el perfil completo. Los slots ausentes (None) simplemente no
        se insertan, de modo que `load` los reconstruye como None y el
        round-trip es lossless para los huecos.

        `now_ms` (I4) queda estampado en `profile_meta.updated_ms`, y el
        `ON CONFLICT` lo actualiza en cada guardado -no solo en el
        primero-: sin esto no había ni siquiera un timestamp del que
        detectar que un perfil calculado en el arranque llevaba semanas sin
        recalcularse. `now_ms` debe venir siempre del reloj del exchange
        (nunca de `time.time()`), igual que el resto del motor."""
        filas = [
            (profile.symbol, m, s.median, s.p75, s.p90, s.p95, s.samples)
            for m, s in enumerate(profile.slots)
            if s is not None
        ]
        self._conn.execute("DELETE FROM volume_profile WHERE symbol = ?", (profile.symbol,))
        self._conn.executemany(
            """INSERT INTO volume_profile
               (symbol, minute_of_day, median, p75, p90, p95, samples)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            filas,
        )
        self._conn.execute(
            """INSERT INTO profile_meta (symbol, confidence, days_covered, updated_ms)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                 confidence=excluded.confidence, days_covered=excluded.days_covered,
                 updated_ms=excluded.updated_ms""",
            (profile.symbol, profile.confidence, profile.days_covered, now_ms),
        )
        self._conn.commit()

    def load(self, symbol: str) -> VolumeProfile | None:
        meta = self._conn.execute(
            "SELECT confidence, days_covered FROM profile_meta WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        if meta is None:
            return None
        slots: list[SlotStats | None] = [None] * MINUTOS_POR_DIA
        for f in self._conn.execute(
            """SELECT minute_of_day, median, p75, p90, p95, samples
               FROM volume_profile WHERE symbol = ?""",
            (symbol,),
        ):
            slots[f["minute_of_day"]] = SlotStats(
                median=f["median"], p75=f["p75"], p90=f["p90"],
                p95=f["p95"], samples=f["samples"],
            )
        return VolumeProfile(
            symbol=symbol,
            slots=tuple(slots),
            confidence=meta["confidence"],
            days_covered=meta["days_covered"],
        )

    def get_updated_ms(self, symbol: str) -> int | None:
        """Edad del perfil persistido (I4 / Finding "perfil rancio al
        entrar"): `Orchestrator._resolver_perfil` la usa para decidir si un
        perfil cargado de disco lleva demasiado tiempo sin recalcularse y
        debe reconstruirse antes de usarse. Separado de `load` -que no
        expone `updated_ms`, solo `confidence`/`days_covered`- porque la
        mayoría de sus llamadores no lo necesitan. `None` si el símbolo no
        tiene perfil guardado, igual que `load`."""
        fila = self._conn.execute(
            "SELECT updated_ms FROM profile_meta WHERE symbol = ?", (symbol,)
        ).fetchone()
        return fila["updated_ms"] if fila is not None else None


class SignalRepo:
    _CAMPOS = (
        "ts", "symbol", "direction", "state", "score",
        "score_momentum", "score_demand", "score_structure", "price",
        "rvol_1m_closed", "rvol_1m_live", "rvol_5m", "rvol_session", "demand_burst",
        "ret_1m", "ret_3m", "ret_5m", "ret_15m", "ret_30m", "ret_1h", "ret_24h",
        "vwap", "vwap_distance", "z_return", "market_cap", "volume_24h",
        "open_interest", "funding_rate", "profile_confidence",
        "config_fingerprint", "code_revision",
    )

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(
        self, metrics: SymbolMetrics, breakdown: ScoreBreakdown, state: State,
        config_fingerprint: str, code_revision: str,
    ) -> int:
        """Guarda la foto completa de métricas en el instante de la señal.
        Cualquier métrica en None se persiste como NULL (nunca como 0), para
        no confundir "no se pudo calcular" con "el valor fue cero".

        `config_fingerprint`/`code_revision` (procedencia, ver
        `scanner_volumen/provenance.py`) son obligatorios -sin valor por
        defecto- a propósito: la ausencia de un default hace imposible
        grabar una señal sin decidir explícitamente qué procedencia lleva,
        que es justo la disciplina que faltaba y que motivó esta tarea."""
        valores = (
            metrics.ts, metrics.symbol, breakdown.direction.value, state.value,
            breakdown.total, breakdown.momentum, breakdown.demand,
            breakdown.structure, metrics.price,
            metrics.rvol_1m_closed, metrics.rvol_1m_live, metrics.rvol_5m,
            metrics.rvol_session, metrics.demand_burst,
            metrics.ret_1m, metrics.ret_3m, metrics.ret_5m, metrics.ret_15m,
            metrics.ret_30m, metrics.ret_1h, metrics.ret_24h,
            metrics.vwap, metrics.vwap_distance, metrics.z_return,
            metrics.market_cap, metrics.volume_24h, metrics.open_interest,
            metrics.funding_rate, metrics.profile_confidence,
            config_fingerprint, code_revision,
        )
        marcadores = ", ".join("?" * len(self._CAMPOS))
        cur = self._conn.execute(
            f"INSERT INTO signals ({', '.join(self._CAMPOS)}) VALUES ({marcadores})",
            valores,
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def recent(self, since_ms: int) -> list[dict]:
        filas = self._conn.execute(
            "SELECT * FROM signals WHERE ts >= ? ORDER BY ts DESC", (since_ms,)
        ).fetchall()
        return [dict(f) for f in filas]

    def all_signals(self) -> list[dict]:
        """Todas las señales, ordenadas por símbolo y luego por ts. Usado por
        la herramienta de backtest (`scanner_volumen/backtest`), que necesita
        el histórico completo -no una ventana reciente como `recent`- y el
        orden por símbolo para agrupar episodios sin tener que reordenar."""
        filas = self._conn.execute(
            "SELECT * FROM signals ORDER BY symbol, ts"
        ).fetchall()
        return [dict(f) for f in filas]

    def all_outcomes(self) -> list[dict]:
        """Todos los resultados registrados en `signal_outcomes`. Usado por
        la herramienta de backtest para cruzar cada señal con su resultado en
        cada horizonte, sin pasar por `pending_outcomes` (que solo mira lo
        que falta, no lo que ya existe)."""
        filas = self._conn.execute("SELECT * FROM signal_outcomes").fetchall()
        return [dict(f) for f in filas]

    def pending_outcomes(
        self, now_ms: int, horizons: tuple[int, ...]
    ) -> list[tuple[int, str, float, int, int]]:
        """Devuelve (signal_id, symbol, precio_entrada, horizonte, ts_señal) de
        los horizontes ya vencidos (ha transcurrido al menos `horizon_min`
        minutos desde la señal) que aún no tienen resultado registrado para
        ESE horizonte concreto."""
        pendientes = []
        for h in horizons:
            limite = now_ms - h * 60_000
            for f in self._conn.execute(
                """SELECT s.id, s.symbol, s.price, s.ts FROM signals s
                   WHERE s.ts <= ? AND s.price IS NOT NULL
                     AND NOT EXISTS (
                       SELECT 1 FROM signal_outcomes o
                       WHERE o.signal_id = s.id AND o.horizon_min = ?)""",
                (limite, h),
            ):
                pendientes.append((f["id"], f["symbol"], f["price"], h, f["ts"]))
        return pendientes

    def save_outcome(
        self, signal_id: int, horizon_min: int, price: float,
        return_pct: float, mfe_pct: float, mae_pct: float,
        candles_seen: int, candles_expected: int,
    ) -> None:
        """`candles_seen`/`candles_expected` registran si la ventana del
        horizonte estaba completa (para que V3 pueda calibrar los pesos del
        score con evidencia limpia). Un hueco de datos (p. ej. una caída de
        WS ya cubierta por `refill_gap`, o un símbolo poco líquido) deja
        `candles_seen < candles_expected`; sin esto, esa fila era
        indistinguible de una ventana completa."""
        self._conn.execute(
            """INSERT INTO signal_outcomes
               (signal_id, horizon_min, price, return_pct, mfe_pct, mae_pct,
                candles_seen, candles_expected)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(signal_id, horizon_min) DO NOTHING""",
            (signal_id, horizon_min, price, return_pct, mfe_pct, mae_pct,
             candles_seen, candles_expected),
        )
        self._conn.commit()


class StateTransitionRepo:
    """Persiste la trayectoria completa de estados (WATCH o superior) de
    cada símbolo -incluidas las transiciones que retroceden a NORMAL, que
    `signals` nunca captura porque solo persiste escaladas a HOT+ (ver
    `Orchestrator.evaluate` y `_persisted_min_state`)-, para poder medir
    después estrategias del tipo "entra en X, sale en Y" (p. ej. entra en
    WATCH, sale en HOT) sin look-ahead, cruzando estos `ts` con las velas ya
    persistidas en `candles_1m`.

    Puramente aditiva y de solo logging (I de la tarea): a diferencia de
    `signals`, esta tabla no tiene ningún `..._outcomes` asociado -el
    análisis posterior calcula los retornos desde `candles_1m`, no desde
    aquí-, y su llamador (`evaluate`) es quien decide qué transiciones
    califican (`prev_state`/`new_state` WATCH o superior); este repositorio
    no repite ese filtro, solo persiste lo que se le pasa."""

    _CAMPOS = (
        "ts", "symbol", "prev_state", "new_state", "score", "price",
        "direction", "escalated", "config_fingerprint", "code_revision",
    )

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(
        self, transition: Transition, price: float | None, direction: Direction,
        config_fingerprint: str, code_revision: str,
    ) -> int:
        """`price`/`direction`/procedencia no los conoce `Transition` -solo
        `symbol`/`previous`/`current`/`score`/`escalated`/`ts`-, así que el
        llamador (`evaluate`) los pasa aparte, tomados de la misma foto de
        métricas y breakdown que usa `SignalRepo.insert` para la señal HOT+,
        con el mismo `config_fingerprint`/`code_revision`, para que ambas
        tablas sean comparables entre sí."""
        valores = (
            transition.ts, transition.symbol, transition.previous.value,
            transition.current.value, transition.score, price,
            direction.value, int(transition.escalated),
            config_fingerprint, code_revision,
        )
        marcadores = ", ".join("?" * len(self._CAMPOS))
        cur = self._conn.execute(
            f"INSERT INTO state_transitions ({', '.join(self._CAMPOS)}) "
            f"VALUES ({marcadores})",
            valores,
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def recent(self, since_ms: int) -> list[dict]:
        filas = self._conn.execute(
            "SELECT * FROM state_transitions WHERE ts >= ? ORDER BY ts DESC",
            (since_ms,),
        ).fetchall()
        return [dict(f) for f in filas]

    def all_transitions(
        self, desde_ms: int | None = None, hasta_ms: int | None = None,
    ) -> list[sqlite3.Row]:
        """Toda la trayectoria persistida, orden cronológico estable (ts y,
        a igualdad de ts, orden de inserción). Solo lectura, para el backtest
        de trayectoria.

        `desde_ms`/`hasta_ms` acotan la ventana (ambos inclusive) para poder
        comparar el backtest con una corrida del bot en vivo que solo cubre
        unas semanas concretas; sin flags (el caso de siempre, incluido el
        golden master) el comportamiento es idéntico al de antes de que
        existieran."""
        condiciones: list[str] = []
        parametros: list[int] = []
        if desde_ms is not None:
            condiciones.append("ts >= ?")
            parametros.append(desde_ms)
        if hasta_ms is not None:
            condiciones.append("ts <= ?")
            parametros.append(hasta_ms)
        where = f" WHERE {' AND '.join(condiciones)}" if condiciones else ""
        return self._conn.execute(
            f"SELECT * FROM state_transitions{where} ORDER BY ts, id", parametros
        ).fetchall()

    def por_simbolo(self, symbol: str, desde_ms: int) -> list[sqlite3.Row]:
        """Trayectoria de un símbolo desde un instante, orden cronológico
        estable. La usa el bot para replicar una posición al reiniciar."""
        return self._conn.execute(
            "SELECT * FROM state_transitions WHERE symbol = ? AND ts >= ? "
            "ORDER BY ts, id",
            (symbol, desde_ms),
        ).fetchall()


class MaintenanceRepo:
    """Persiste cuándo completó trabajo real por última vez `Orchestrator.
    run_maintenance` (poda I2 + recálculo de perfil I4), para que
    `paso_mantenimiento` (__main__.py) decida si el mantenimiento diario
    está vencido sin depender de cuánto lleva vivo el proceso actual.

    Bajo systemd con `Restart=always`, un proceso puede reiniciarse antes
    de acumular `interval_hours` seguidas de vida; sin esta persistencia,
    `bucle_mantenimiento` dormía la cadencia completa ANTES de correr nada,
    así que un proceso que se reinicia con esa frecuencia no llegaba a
    correr mantenimiento nunca -medido en real: los perfiles de volumen
    más viejos llevaban seis días sin recalcularse-.

    Tabla dedicada de una sola fila (`maintenance_meta`, ver storage/db.py),
    no `MAX(profile_meta.updated_ms)`: esa columna también la actualiza
    `Bootstrapper.bootstrap_symbol` en cada alta normal de universo (no solo
    el mantenimiento diario), así que su máximo confundiría "se bootstrapeó
    un símbolo nuevo" con "corrió el ciclo de mantenimiento completo"."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_last_completed_ms(self) -> int | None:
        fila = self._conn.execute(
            "SELECT last_completed_ms FROM maintenance_meta WHERE id = 1"
        ).fetchone()
        return fila["last_completed_ms"] if fila is not None else None

    def set_last_completed_ms(self, now_ms: int) -> None:
        """`now_ms` debe venir siempre del reloj del exchange (nunca de
        `time.time()`), igual que el resto del motor. El llamador
        (`paso_mantenimiento`) es responsable de invocar esto solo cuando
        `run_maintenance` hizo trabajo real -ver su docstring sobre el
        no-op de arranque en frío-, nunca en cada vencimiento."""
        self._conn.execute(
            """INSERT INTO maintenance_meta (id, last_completed_ms) VALUES (1, ?)
               ON CONFLICT(id) DO UPDATE SET last_completed_ms = excluded.last_completed_ms""",
            (now_ms,),
        )
        self._conn.commit()


class SupplyRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(
        self, symbol: str, coingecko_id: str | None,
        circulating_supply: float | None, market_cap: float | None,
        fdv: float | None, updated_ms: int,
    ) -> None:
        self._conn.execute(
            """INSERT INTO supply_cache
               (symbol, coingecko_id, circulating_supply, market_cap, fdv, updated_ms)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                 coingecko_id=excluded.coingecko_id,
                 circulating_supply=excluded.circulating_supply,
                 market_cap=excluded.market_cap,
                 fdv=excluded.fdv,
                 updated_ms=excluded.updated_ms""",
            (symbol, coingecko_id, circulating_supply, market_cap, fdv, updated_ms),
        )
        self._conn.commit()

    def load_all(self) -> dict[str, float]:
        return {
            f["symbol"]: f["market_cap"]
            for f in self._conn.execute(
                "SELECT symbol, market_cap FROM supply_cache WHERE market_cap IS NOT NULL"
            )
        }
