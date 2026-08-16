# scanner_volumen/engine/candles.py
"""Buffer de velas de 1 minuto por símbolo.

Separa explícitamente la vela en curso de las cerradas: el RVOL de una vela
incompleta no es comparable al de una completa, y mezclarlas es la fuente de
error más fácil de cometer en todo el sistema.
"""
from __future__ import annotations

from collections import deque

from scanner_volumen.models import Candle

MINUTO_MS = 60_000


class CandleBuffer:
    def __init__(self, symbol: str, capacity: int = 1500) -> None:
        self.symbol = symbol
        self.capacity = capacity
        self._closed: deque[Candle] = deque(maxlen=capacity)
        self._current: Candle | None = None
        self._by_ts: dict[int, Candle] = {}

    def upsert(self, candle: Candle) -> bool:
        """Devuelve True si abre una vela nueva, False si actualiza la actual
        o si la vela es más antigua que la actual (se ignora)."""
        if self._current is None:
            self._current = candle
            return True
        if candle.ts == self._current.ts:
            self._current = candle
            return False
        if candle.ts < self._current.ts:
            return False

        self._cerrar_actual()
        self._current = candle
        return True

    def _cerrar_actual(self) -> None:
        if self._current is None:
            return
        if len(self._closed) == self._closed.maxlen:
            self._by_ts.pop(self._closed[0].ts, None)
        self._closed.append(self._current)
        self._by_ts[self._current.ts] = self._current

    def current(self) -> Candle | None:
        return self._current

    def closed(self, n: int) -> list[Candle]:
        if n <= 0:
            return []
        return list(self._closed)[-n:]

    def all_closed(self) -> list[Candle]:
        return list(self._closed)

    def close_at(self, minutes_ago: int) -> float | None:
        """Cierre de hace `minutes_ago` minutos respecto a la vela en curso.

        Devuelve None si esa vela concreta no está en el buffer: un hueco en el
        histórico no debe sustituirse por el cierre de un minuto vecino.
        """
        if self._current is None:
            return None
        if minutes_ago == 0:
            return self._current.close
        objetivo = self._current.ts - minutes_ago * MINUTO_MS
        vela = self._by_ts.get(objetivo)
        return vela.close if vela is not None else None

    def session_volume(self, day_start_ms: int) -> float:
        """Volumen en quote (USDT) acumulado desde el inicio de la sesión.

        Todo el motor trabaja en volumen quote (USDT), no en moneda base:
        el perfil de volumen y rvol_window también se calculan sobre
        quote_vol, así que session_volume debe usar la misma unidad para
        que rvol_session = session_volume / baseline sea comparable.

        Incluye la vela en curso: su volumen parcial sigue siendo volumen
        real ya operado, aunque no sea comparable a una vela cerrada.
        """
        total = sum(c.quote_vol for c in self._closed if c.ts >= day_start_ms)
        if self._current is not None and self._current.ts >= day_start_ms:
            total += self._current.quote_vol
        return total

    def backfill(self, candles: list[Candle]) -> int:
        """Inserta velas históricas (p. ej. leídas de SQLite en el arranque)
        sin tocar la vela en curso.

        A diferencia de `upsert`, que descarta cualquier vela más antigua que
        `_current` para que un mensaje de WS tardío no reabra una vela ya
        cerrada, `backfill` es la puerta explícita para sembrar el histórico:
        solo admite velas estrictamente anteriores a `_current` (o cualquier
        vela si aún no hay vela en curso), ignora las que ya están en el
        buffer y respeta la capacidad, descartando las más antiguas si no
        caben todas. Devuelve cuántas quedaron efectivamente insertadas.
        """
        limite = self._current.ts if self._current is not None else None
        existentes = {c.ts for c in self._closed}
        candidatas: dict[int, Candle] = {}
        for vela in candles:
            if limite is not None and vela.ts >= limite:
                continue
            if vela.ts in existentes:
                continue
            candidatas[vela.ts] = vela
        if not candidatas:
            return 0

        combinadas = sorted([*self._closed, *candidatas.values()], key=lambda c: c.ts)
        maxlen = self._closed.maxlen
        if maxlen is not None and len(combinadas) > maxlen:
            combinadas = combinadas[-maxlen:]

        ts_finales = {c.ts for c in combinadas}
        insertadas = sum(1 for ts in candidatas if ts in ts_finales)

        self._closed = deque(combinadas, maxlen=maxlen)
        self._by_ts = {c.ts: c for c in combinadas}
        return insertadas
