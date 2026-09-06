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

    def __repr__(self) -> str:
        """Representación que no expone las credenciales."""
        return f"BitgetPrivate(venue={self._venue!r})"

    async def _pedir(self, method: str, path: str, params: dict[str, str] | None = None) -> dict:
        """Realiza una petición autenticada a Bitget.

        Comprueba que code == "00000", sino lanza RuntimeError sin exponer credenciales.

        IMPORTANTE: La cadena de consulta se construye una sola vez y se usa tanto para
        la firma como para la URL. Esto garantiza que lo que se firma es exactamente
        lo que se envía. Para POST/PUT con cuerpo, aplicar el mismo principio:
        serializar una sola vez y usar esa cadena exacta en la firma y en el envío
        (via content= de httpx, nunca json=).
        """
        await self._bucket.acquire()
        todos = {**self._product_params, **(params or {})}

        # Forzar el método a mayúsculas (spec de Bitget)
        metodo_mayusculas = method.upper()
        timestamp = str(int(time.time() * 1000))

        # Construir la cadena de consulta UNA SOLA VEZ para usarla en firma y URL
        # Si no hay parámetros, la parte extra debe ser cadena vacía (no "?")
        query_string = ""
        if metodo_mayusculas == "GET" and todos:
            # Sorted para garantizar orden consistente
            # Nota: aquí se asume que los valores no necesitan codificación URL.
            # Si aparecen valores con caracteres especiales, usar urllib.parse.urlencode
            # pero entonces hay que asegurarse de que la codificación se usa en ambas cosas.
            query_string = "&".join(f"{k}={v}" for k, v in sorted(todos.items()))
            query_string = "?" + query_string

        # paramsStr = timestamp + METODO + ruta + extra
        params_str = timestamp + metodo_mayusculas + path + query_string

        # firma = base64(HMAC-SHA256(secreto, paramsStr))
        firma = base64.b64encode(
            hmac.new(
                self._api_secret.encode(),
                params_str.encode(),
                hashlib.sha256,
            ).digest()
        ).decode()

        # Construir la URL final con la cadena de consulta ya formada
        url = f"{BASE_URL}{path}{query_string}"

        headers = {
            "ACCESS-KEY": self._api_key,
            "ACCESS-SIGN": firma,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": self._passphrase,
            "Content-Type": "application/json",
        }

        # NO pasar params= en GET; la URL ya contiene la query string
        resp = await self._client.request(
            metodo_mayusculas,
            url,
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
        """Obtiene el saldo de la subcuenta entera (plural, todas las monedas).

        Endpoint plural `/api/v2/mix/account/accounts` devuelve una lista de cuentas,
        una por moneda de margen. Esta tarea selecciona explícitamente la de USDT,
        que es la moneda sobre la que dimensiona el bot.

        El `realizado` es accountEquity - unrealizedPL, la cifra que replica
        la semántica del backtest.
        """
        payload = await self._pedir("GET", "/api/v2/mix/account/accounts")
        data_list = payload.get("data", [])

        # Seleccionar explícitamente la cuenta en USDT
        data = None
        for item in data_list:
            if item.get("marginCoin") == "USDT":
                data = item
                break

        if data is None:
            raise RuntimeError("No se encontró saldo en USDT en Bitget")

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
