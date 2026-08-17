"""Token bucket asíncrono para no exceder los límites de tasa de Bitget."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


class TokenBucket:
    """El reloj y la función de espera se inyectan para poder testear sin dormir."""

    def __init__(
        self,
        rate_per_second: float,
        now: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second debe ser mayor que cero")
        self._rate = rate_per_second
        self._capacity = rate_per_second
        self._tokens = rate_per_second
        self._now = now or (lambda: asyncio.get_running_loop().time())
        self._sleep = sleep or asyncio.sleep
        self._last = self._now()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            self._recargar()
            if self._tokens < 1:
                faltan = 1 - self._tokens
                await self._sleep(faltan / self._rate)
                self._recargar()
            self._tokens -= 1

    def _recargar(self) -> None:
        ahora = self._now()
        transcurrido = max(0.0, ahora - self._last)
        self._last = ahora
        self._tokens = min(self._capacity, self._tokens + transcurrido * self._rate)
