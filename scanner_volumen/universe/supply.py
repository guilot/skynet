# scanner_volumen/universe/supply.py
"""Cache de market cap a partir de CoinGecko.

Deliberadamente no bloqueante: si CoinGecko falla, está limitado por tasa o no
conoce un token, el símbolo sigue en el universo y simplemente puntúa 0 en el
componente de capitalización. El scanner no depende de una API externa.
"""
from __future__ import annotations

import logging

import httpx

from scanner_volumen.storage.repos import SupplyRepo

URL_MERCADOS = "https://api.coingecko.com/api/v3/coins/markets"
PAGINAS = 4
POR_PAGINA = 250

log = logging.getLogger(__name__)


def base_coin_of(symbol: str) -> str:
    """Quita el sufijo USDT de un símbolo de perp de Bitget, p. ej.
    "BTCUSDT" -> "BTC". Símbolos sin ese sufijo se devuelven sin cambios."""
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def match_coingecko(base_coin: str, coins: list[dict]) -> dict | None:
    """CoinGecko contiene muchos tokens con el mismo ticker. Se elige el de
    mayor capitalización, que es el que corresponde al listado en un exchange
    grande. Es una heurística: la spec (§13) lista un override manual de
    `supply_cache` como mitigación para un mapeo ambiguo, pero esa columna
    no existe hoy -- ni aquí ni en el esquema de `supply_cache`
    (`storage/db.py`) hay forma de forzar manualmente qué moneda de
    CoinGecko corresponde a un símbolo. Un símbolo mal emparejado por esta
    heurística no tiene hoy ninguna vía de corrección salvo editar el código.
    """
    objetivo = base_coin.lower()
    candidatos = [c for c in coins if str(c.get("symbol", "")).lower() == objetivo]
    if not candidatos:
        return None
    return max(candidatos, key=lambda c: c.get("market_cap") or 0.0)


class SupplyCache:
    """Cache en memoria (respaldada en SQLite) de market cap por símbolo.

    `refresh` nunca lanza: cualquier fallo de CoinGecko se registra y se
    ignora, devolviendo cuántos símbolos se pudieron actualizar (0 si nada).
    Un fallo NO marca la cache como recién refrescada, para no bloquear el
    reintento durante `refresh_hours` por culpa de un fallo pasajero.
    """

    def __init__(
        self, repo: SupplyRepo, client: httpx.AsyncClient, refresh_hours: int = 6
    ) -> None:
        self._repo = repo
        self._client = client
        self._refresh_ms = refresh_hours * 3_600_000
        self._caps: dict[str, float] = repo.load_all()
        self._last_refresh_ms: int | None = None

    def market_cap(self, symbol: str) -> float | None:
        return self._caps.get(symbol)

    async def refresh(self, symbols: list[str], now_ms: int) -> int:
        """Refresca las capitalizaciones de `symbols` contra CoinGecko, salvo
        que ya se haya refrescado con éxito dentro de `refresh_hours`. `now_ms`
        viene siempre del exchange, nunca de time.time()."""
        if (
            self._last_refresh_ms is not None
            and now_ms - self._last_refresh_ms < self._refresh_ms
        ):
            return 0

        mercados = await self._descargar()
        if not mercados:
            # Fallo o respuesta vacía: no se marca como refrescada, para que
            # la próxima llamada reintente sin esperar refresh_hours.
            return 0

        actualizados = 0
        for simbolo in symbols:
            coin = match_coingecko(base_coin_of(simbolo), mercados)
            if coin is None:
                continue
            cap = coin.get("market_cap")
            if cap is None:
                continue
            self._repo.upsert(
                simbolo,
                coin.get("id"),
                coin.get("circulating_supply"),
                float(cap),
                coin.get("fully_diluted_valuation"),
                now_ms,
            )
            self._caps[simbolo] = float(cap)
            actualizados += 1

        self._last_refresh_ms = now_ms
        return actualizados

    async def _descargar(self) -> list[dict]:
        """Descarga las páginas de /coins/markets. Cualquier excepción (HTTP,
        timeout, JSON inválido) se registra y se traga: nunca debe bloquear
        al scanner por una dependencia externa."""
        todos: list[dict] = []
        for pagina in range(1, PAGINAS + 1):
            try:
                resp = await self._client.get(
                    URL_MERCADOS,
                    params={
                        "vs_currency": "usd",
                        "order": "market_cap_desc",
                        "per_page": POR_PAGINA,
                        "page": pagina,
                    },
                )
                resp.raise_for_status()
                datos = resp.json()
            except Exception as exc:  # noqa: BLE001 - nunca bloquea el scanner
                log.warning("CoinGecko no disponible (%s); se usa la cache previa", exc)
                break
            if not isinstance(datos, list) or not datos:
                break
            todos.extend(datos)
        return todos
