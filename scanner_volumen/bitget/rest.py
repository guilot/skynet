"""Cliente REST de Bitget con limitación de tasa.

El mercado (spot o futuros) es un parámetro: cambia la ruta base, el nombre de
la granularidad y si hay que enviar productType.
"""
from __future__ import annotations

import httpx

from scanner_volumen.bitget.parsing import parse_candles, parse_contracts, parse_tickers
from scanner_volumen.bitget.rate_limit import TokenBucket
from scanner_volumen.models import Candle, Contract, Ticker

BASE_URL = "https://api.bitget.com"
MAX_HISTORY_LIMIT = 200  # verificado: 300 y 1000 devuelven error 40020


class BitgetRest:
    def __init__(self, venue: str, rate_limit: float, client: httpx.AsyncClient) -> None:
        self._venue = venue
        self._client = client
        self._bucket = TokenBucket(rate_limit)
        if venue == "SPOT":
            self._prefix = "/api/v2/spot/market"
            self._granularity = "1min"
            self._product_params: dict[str, str] = {}
        else:
            self._prefix = "/api/v2/mix/market"
            self._granularity = "1m"
            self._product_params = {"productType": venue}

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict:
        await self._bucket.acquire()
        todos = {**self._product_params, **(params or {})}
        resp = await self._client.get(f"{self._prefix}/{path}", params=todos)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != "00000":
            raise RuntimeError(
                f"Bitget devolvió code={payload.get('code')} msg={payload.get('msg')} en {path}"
            )
        return payload

    async def get_contracts(self) -> list[Contract]:
        # parse_contracts espera las claves de futuros (symbolType, symbolStatus,
        # isRwa). El endpoint "symbols" de spot queda enrutado aquí para cuando
        # se necesite, pero su parseo no está en el alcance de V1 (solo futuros).
        endpoint = "symbols" if self._venue == "SPOT" else "contracts"
        return parse_contracts(await self._get(endpoint))

    async def get_tickers(self) -> list[Ticker]:
        return parse_tickers(await self._get("tickers"))

    async def get_candles(self, symbol: str, limit: int = 200) -> list[Candle]:
        params = {"symbol": symbol, "granularity": self._granularity, "limit": str(limit)}
        return parse_candles(await self._get("candles", params))

    async def get_history_candles(
        self, symbol: str, end_time_ms: int, limit: int = MAX_HISTORY_LIMIT
    ) -> list[Candle]:
        params = {
            "symbol": symbol,
            "granularity": self._granularity,
            "limit": str(min(limit, MAX_HISTORY_LIMIT)),
            "endTime": str(end_time_ms),
        }
        return parse_candles(await self._get("history-candles", params))
