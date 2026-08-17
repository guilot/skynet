# scanner_volumen/engine/metrics.py
"""Ensambla todas las metricas de un simbolo en una unica estructura.

Es el punto donde convergen buffer, perfil y ticker. Mantiene un pequeno
historial de RVOL por simbolo, necesario para el demand burst, que compara con
el valor de hace cinco minutos. El historial guarda como mucho una muestra
por vela cerrada (estampada por su ts, no por el reloj de pared en que se
evaluo): compute() puede llamarse varias veces por minuto, pero una vela
cerrada solo produce un rvol_1m_closed una vez.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from scanner_volumen.config import EngineConfig, ProfileConfig
from scanner_volumen.engine.burst import demand_burst, z_return
from scanner_volumen.engine.candles import CandleBuffer
from scanner_volumen.engine.momentum import pct_return, returns, vwap, vwap_distance
from scanner_volumen.engine.profile import (
    VolumeProfile, minute_of_day, rolling_baseline,
)
from scanner_volumen.engine.rvol import (
    rvol_closed, rvol_live, rvol_session, rvol_window,
)
from scanner_volumen.models import Ticker

MINUTO_MS = 60_000
DIA_MS = 1440 * MINUTO_MS
# Dos horas de historial, una muestra por vela cerrada (una por minuto).
RVOL_HISTORY_MINUTES = 120


@dataclass(frozen=True)
class SymbolMetrics:
    symbol: str
    price: float | None
    ret_1m: float | None
    ret_3m: float | None
    ret_5m: float | None
    ret_15m: float | None
    ret_30m: float | None
    ret_1h: float | None
    ret_24h: float | None
    rvol_1m_closed: float | None
    rvol_1m_live: float | None
    rvol_5m: float | None
    rvol_session: float | None
    demand_burst: float | None
    vwap: float | None
    vwap_distance: float | None
    z_return: float | None
    market_cap: float | None
    volume_24h: float | None
    open_interest: float | None
    funding_rate: float | None
    profile_confidence: str
    ts: int


class MetricsBuilder:
    def __init__(self, engine_cfg: EngineConfig, profile_cfg: ProfileConfig) -> None:
        self._engine = engine_cfg
        self._profile_cfg = profile_cfg
        # simbolo -> deque de (ts_ms, rvol_1m_closed)
        self._rvol_history: dict[str, deque[tuple[int, float]]] = {}

    def forget(self, symbol: str) -> None:
        """Descarta el historial de RVOL de un simbolo que salio del universo.

        `apply_universe` ya limpia buffers, perfiles y placeholders de un
        simbolo eliminado; sin esto, `_rvol_history` seguiria creciendo sin
        limite con cada simbolo que entra y sale del universo a lo largo de
        dias de operacion, aunque ese simbolo ya no se evalue nunca mas.
        """
        self._rvol_history.pop(symbol, None)

    def record_rvol(self, symbol: str, rvol: float, ts: int) -> None:
        """Registra una muestra de RVOL cerrado, estampada por el ts (ms) de
        la vela que la produjo -- nunca por el reloj de pared -- y
        deduplicada: `compute()` puede llamarse mas de una vez sobre la misma
        vela cerrada (p.ej. un ticker que llega sin vela nueva la deja
        "sucia" otra vez), y sin este chequeo cada llamada añadiria una copia
        identica del mismo minuto en vez de como mucho una.
        """
        hist = self._rvol_history.setdefault(symbol, deque(maxlen=RVOL_HISTORY_MINUTES))
        if hist and hist[-1][0] == ts:
            return
        hist.append((ts, rvol))

    def _rvol_hace(self, symbol: str, ts_referencia: int, minutos: int) -> float | None:
        """Valor de RVOL mas cercano a `minutos` atras de `ts_referencia` (ts
        de vela cerrada, no reloj de pared), con tolerancia de +-1 min.

        Con ambos lados alineados al minuto -- ts de vela contra ts de vela --
        la tolerancia ya no absorbe jitter de reloj (eso ya no existe: las
        muestras solo caen en multiplos exactos de un minuto). Sigue teniendo
        sentido igualmente: tolera como mucho una vela perdida justo en el
        objetivo (p.ej. un hueco de reconexion que aun no paso por
        refill_gap), usando la vela adyacente en vez de devolver None por la
        falta de una unica muestra.
        """
        hist = self._rvol_history.get(symbol)
        if not hist:
            return None
        objetivo = ts_referencia - minutos * MINUTO_MS
        mejor: tuple[int, float] | None = None
        for ts, valor in hist:
            if abs(ts - objetivo) <= MINUTO_MS:
                if mejor is None or abs(ts - objetivo) < abs(mejor[0] - objetivo):
                    mejor = (ts, valor)
        return mejor[1] if mejor else None

    def _baseline(self, profile: VolumeProfile, buffer: CandleBuffer, ts: int) -> float | None:
        """Con perfil poco fiable se recurre a la mediana rolling del propio simbolo."""
        if profile.confidence == "high":
            return profile.baseline(minute_of_day(ts))
        return rolling_baseline(
            buffer.all_closed(), self._profile_cfg.rolling_fallback_candles
        )

    def compute(
        self,
        symbol: str,
        buffer: CandleBuffer,
        profile: VolumeProfile,
        ticker: Ticker | None,
        market_cap: float | None,
        now_ms: int,
    ) -> SymbolMetrics:
        actual = buffer.current()
        cerradas = buffer.closed(5)
        precio = actual.close if actual is not None else None

        ultima_cerrada = cerradas[-1] if cerradas else None
        rvol_cerrado = None
        if ultima_cerrada is not None:
            rvol_cerrado = rvol_closed(
                ultima_cerrada.quote_vol,
                self._baseline(profile, buffer, ultima_cerrada.ts),
            )

        rvol_vivo = None
        if actual is not None:
            transcurrido = max(0.0, (now_ms - actual.ts) / 1000.0)
            rvol_vivo = rvol_live(
                actual.quote_vol,
                self._baseline(profile, buffer, actual.ts),
                transcurrido,
                self._engine.live_rvol_min_elapsed_seconds,
            )

        rvol_5m = rvol_window(cerradas, profile) if profile.confidence == "high" else None
        if rvol_5m is None and cerradas:
            base = rolling_baseline(
                buffer.all_closed(), self._profile_cfg.rolling_fallback_candles
            )
            if base is not None:
                rvol_5m = sum(c.quote_vol for c in cerradas) / (base * len(cerradas))

        inicio_dia = (now_ms // DIA_MS) * DIA_MS
        minuto_actual = minute_of_day(now_ms)
        # A diferencia de rvol_1m/rvol_5m, aqui NO hay fallback a rolling_baseline
        # cuando la confianza es baja, y es deliberado: rvol_session compara el
        # volumen acumulado desde las 00:00 UTC contra una sesion historica
        # "normal". Un simbolo con poco historial (listing nuevo) no tiene
        # ninguna sesion pasada con la que compararse; sintetizar una
        # multiplicando una mediana rolling por los minutos transcurridos
        # fabricaria un baseline para una sesion que el simbolo nunca vivio,
        # produciendo un numero con apariencia fiable pero sin fundamento real.
        # None es la respuesta honesta aqui: el simbolo pierde solo los 5 puntos
        # del componente rvol_session en el score, pero se sigue escaneando y
        # puntuando por rvol_1m/5m, momentum, VWAP y burst.
        rvol_ses = rvol_session(
            buffer.session_volume(inicio_dia),
            profile.cumulative_baseline(minuto_actual)
            if profile.confidence == "high"
            else None,
        )

        rets = returns(buffer, horizons=(1, 3, 5, 15, 30, 60))

        del_dia = [c for c in buffer.all_closed() if c.ts >= inicio_dia]
        if actual is not None and actual.ts >= inicio_dia:
            del_dia = [*del_dia, actual]
        valor_vwap = vwap(del_dia)

        ventana = buffer.closed(self._engine.zscore_window_minutes + 1)
        retornos_recientes = [
            r
            for r in (
                pct_return(ventana[i + 1].close, ventana[i].close)
                for i in range(len(ventana) - 1)
            )
            if r is not None
        ]
        z = (
            z_return(retornos_recientes, rets[1], min_samples=self._engine.zscore_min_samples)
            if rets[1] is not None
            else None
        )

        burst = None
        if ultima_cerrada is not None:
            burst = demand_burst(
                rvol_cerrado,
                self._rvol_hace(
                    symbol, ultima_cerrada.ts, self._engine.burst_lookback_minutes
                ),
                min_denominator=self._engine.demand_burst_min_denominator,
            )
            # se registra despues de consultar el historial, para que la
            # propia muestra de esta vela no pueda emparejarse consigo misma.
            if rvol_cerrado is not None:
                self.record_rvol(symbol, rvol_cerrado, ultima_cerrada.ts)

        return SymbolMetrics(
            symbol=symbol,
            price=precio,
            ret_1m=rets[1], ret_3m=rets[3], ret_5m=rets[5],
            ret_15m=rets[15], ret_30m=rets[30], ret_1h=rets[60],
            ret_24h=ticker.change_24h if ticker else None,
            rvol_1m_closed=rvol_cerrado,
            rvol_1m_live=rvol_vivo,
            rvol_5m=rvol_5m,
            rvol_session=rvol_ses,
            demand_burst=burst,
            vwap=valor_vwap,
            vwap_distance=vwap_distance(precio, valor_vwap) if precio is not None else None,
            z_return=z,
            market_cap=market_cap,
            volume_24h=ticker.volume_24h_usdt if ticker else None,
            open_interest=ticker.open_interest if ticker else None,
            funding_rate=ticker.funding_rate if ticker else None,
            profile_confidence=profile.confidence,
            ts=now_ms,
        )
