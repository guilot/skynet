# scanner_volumen/storage/repos.py
"""Acceso a datos. Aísla el resto del sistema del motor de base de datos:
migrar a PostgreSQL en V3 debería tocar solo este fichero.
"""
from __future__ import annotations

import sqlite3

from scanner_volumen.engine.metrics import SymbolMetrics
from scanner_volumen.engine.profile import MINUTOS_POR_DIA, SlotStats, VolumeProfile
from scanner_volumen.models import Candle, State
from scanner_volumen.scoring.score import ScoreBreakdown


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


class SignalRepo:
    _CAMPOS = (
        "ts", "symbol", "direction", "state", "score",
        "score_momentum", "score_demand", "score_structure", "price",
        "rvol_1m_closed", "rvol_1m_live", "rvol_5m", "rvol_session", "demand_burst",
        "ret_1m", "ret_3m", "ret_5m", "ret_15m", "ret_30m", "ret_1h", "ret_24h",
        "vwap", "vwap_distance", "z_return", "market_cap", "volume_24h",
        "open_interest", "funding_rate", "profile_confidence",
    )

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(
        self, metrics: SymbolMetrics, breakdown: ScoreBreakdown, state: State
    ) -> int:
        """Guarda la foto completa de métricas en el instante de la señal.
        Cualquier métrica en None se persiste como NULL (nunca como 0), para
        no confundir "no se pudo calcular" con "el valor fue cero"."""
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
