"""Cliente privado (autenticado) de Bitget para operaciones con dinero real.

Este módulo está separado de rest.py porque la autenticación requiere credenciales
(clave, secreto, passphrase) que el cliente público no necesita cargar. Esta separación
evita que el código del scanner sea una dependencia de claves reales.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass

import httpx

from scanner_volumen.bitget.rate_limit import TokenBucket

BASE_URL = "https://api.bitget.com"


@dataclass(frozen=True)
class SaldoCuenta:
    """Saldo de una cuenta de futuros en Bitget.

    El `realizado` es la cifra key: accountEquity - unrealizedPL. Es la única que
    replica la semántica del backtest, donde el pnl no realizado no afecta el capital
    disponible para nuevas operaciones.
    """
    realizado: float
    disponible: float
    equity: float
    pnl_no_realizado: float


@dataclass(frozen=True)
class PosicionExchange:
    """Posición abierta en el exchange."""
    symbol: str
    lado: str  # "long" o "short"
    tamano: float
    precio_entrada: float


class BitgetPrivate:
    """Cliente autenticado para Bitget con firma de peticiones.

    Usa el mismo patrón que BitgetRest: rate limiting vía TokenBucket,
    manejo de errores uniforme.
    """

    def __init__(
        self,
        venue: str,
        rate_limit: float,
        client: httpx.AsyncClient,
        api_key: str,
        api_secret: str,
        passphrase: str,
    ) -> None:
        self._venue = venue
        self._client = client
        self._bucket = TokenBucket(rate_limit)
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._product_params = {"productType": venue}

    async def _pedir(self, method: str, path: str, params: dict[str, str] | None = None) -> dict:
        """Realiza una petición autenticada a Bitget.

        Comprueba que code == "00000", sino lanza RuntimeError sin exponer credenciales.
        """
        await self._bucket.acquire()
        todos = {**self._product_params, **(params or {})}

        url = f"{BASE_URL}{path}"
        timestamp = str(int(time.time() * 1000))

        # Construir la firma según el spec de Bitget
        query_string = ""
        if method == "GET" and todos:
            query_string = "&".join(f"{k}={v}" for k, v in sorted(todos.items()))
            query_string = "?" + query_string

        # paramsStr = timestamp + METODO + ruta + extra
        params_str = timestamp + method + path + query_string

        # firma = base64(HMAC-SHA256(secreto, paramsStr))
        firma = base64.b64encode(
            hmac.new(
                self._api_secret.encode(),
                params_str.encode(),
                hashlib.sha256,
            ).digest()
        ).decode()

        headers = {
            "ACCESS-KEY": self._api_key,
            "ACCESS-SIGN": firma,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": self._passphrase,
            "Content-Type": "application/json",
        }

        resp = await self._client.request(
            method,
            url,
            params=todos if method == "GET" else None,
            headers=headers,
        )
        resp.raise_for_status()
        payload = resp.json()

        if payload.get("code") != "00000":
            # NO incluir credenciales en el mensaje de error
            raise RuntimeError(
                f"Bitget devolvió code={payload.get('code')} msg={payload.get('msg')} "
                f"en {path}"
            )

        return payload

    async def get_saldo(self) -> SaldoCuenta:
        """Obtiene el saldo de la cuenta.

        El `realizado` es accountEquity - unrealizedPL, la cifra que replica
        la semántica del backtest.
        """
        payload = await self._pedir("GET", "/api/v2/mix/account/account")
        data_list = payload.get("data", [])
        data = data_list[0] if data_list else {}

        equity = float(data.get("accountEquity", 0))
        pnl_no_realizado = float(data.get("unrealizedPL", 0))
        disponible = float(data.get("available", 0))

        return SaldoCuenta(
            realizado=equity - pnl_no_realizado,
            disponible=disponible,
            equity=equity,
            pnl_no_realizado=pnl_no_realizado,
        )

    async def get_posiciones(self) -> list[PosicionExchange]:
        """Obtiene todas las posiciones abiertas."""
        payload = await self._pedir("GET", "/api/v2/mix/position/all-position")
        posiciones = []

        for item in payload.get("data", []):
            posiciones.append(
                PosicionExchange(
                    symbol=item.get("symbol", ""),
                    lado=item.get("holdSide", "").lower(),
                    tamano=float(item.get("total", 0)),
                    precio_entrada=float(item.get("openPriceAvg", 0)),
                )
            )

        return posiciones
