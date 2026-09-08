"""Cliente privado (autenticado) de Bitget para operaciones con dinero real.

Este módulo está separado de rest.py porque la autenticación requiere credenciales
(clave, secreto, passphrase) que el cliente público no necesita cargar. Esta separación
evita que el código del scanner sea una dependencia de claves reales.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
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


@dataclass(frozen=True)
class ConfiguracionCuentaSymbol:
    """Configuración de margen y apalancamiento de UN símbolo en Bitget
    (Task 11). En Bitget el apalancamiento no es una propiedad de la cuenta:
    es por `symbol` + `marginCoin` + `holdSide`, así que esta consulta se
    hace símbolo a símbolo, nunca una vez "para toda la cuenta" (ver
    `scanner_volumen.bot.verificacion_cuenta`, que es quien decide qué hacer
    con esta información -este cliente solo la reporta, fielmente).

    SUPUESTO SIN VERIFICAR (pendiente de confirmar contra la cuenta de
    simulación en la Task 12): el endpoint candidato es
    `GET /api/v2/mix/account/account` (singular, con `symbol` + `marginCoin`
    + `productType`). Se asume que devuelve `marginMode` con valores
    "isolated" / "crossed", y que el apalancamiento aislado viaja en dos
    campos separados, `isolatedLongLever` e `isolatedShortLever` -Bitget
    permite apalancamiento distinto por lado en margen aislado-. Ninguno de
    estos tres nombres de campo se ha probado contra la API real.
    """
    margen_aislado: bool
    apalancamiento_long: float
    apalancamiento_short: float


@dataclass(frozen=True)
class FillOrden:
    """Resultado agregado de los fills reales de una orden.

    Bitget puede fragmentar una orden a mercado en varios fills parciales. Se
    agregan aquí en un único precio medio ponderado por cantidad y una
    comisión total: es lo que `BitgetBroker` necesita para construir la
    `OrdenEjecutada` a partir de lo que el mercado dio de verdad, no de lo
    que se pidió.
    """
    precio: float
    cantidad: float
    comision: float


def _formato_decimal(valor: float) -> str:
    """Formatea un número para el cuerpo de una petición a Bitget.

    Bitget espera los campos numéricos (tamaños, precios) como cadenas
    decimales, no en notación científica. `repr`/`str` de un float puede caer
    en notación científica para valores muy pequeños o muy grandes; este
    formato evita eso y recorta ceros sobrantes.
    """
    formateado = f"{valor:.10f}".rstrip("0").rstrip(".")
    return formateado if formateado else "0"


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

    async def _pedir(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        body: dict | None = None,
    ) -> dict:
        """Realiza una petición autenticada a Bitget.

        Comprueba que code == "00000", sino lanza RuntimeError sin exponer credenciales.

        IMPORTANTE: tanto la cadena de consulta (GET) como el cuerpo JSON (POST) se
        construyen UNA SOLA VEZ y esa misma cadena se usa para firmar y para enviar.
        Esto garantiza identidad byte a byte entre lo firmado y lo enviado -la causa
        número uno de firmas rechazadas es serializar dos veces (una para firmar, otra
        al enviar) y que el resultado no coincida-. Por eso el cuerpo se manda con
        `content=` y nunca con `json=` de httpx, que volvería a serializar.

        `params` solo se usa en GET (query string); `body` solo en POST/PUT (cuerpo
        JSON). No se mezclan: Bitget firma la query en GET y el cuerpo en POST, nunca
        ambos a la vez en los endpoints que usa este cliente.
        """
        await self._bucket.acquire()

        # Forzar el método a mayúsculas (spec de Bitget)
        metodo_mayusculas = method.upper()
        timestamp = str(int(time.time() * 1000))

        # Construir la cadena de consulta o el cuerpo UNA SOLA VEZ para usarlos
        # tanto en la firma como en la petición real.
        query_string = ""
        cuerpo_str = ""
        if metodo_mayusculas == "GET":
            todos = {**self._product_params, **(params or {})}
            if todos:
                # Sorted para garantizar orden consistente entre firma y envío.
                # Nota: aquí se asume que los valores no necesitan codificación URL.
                # Si aparecen valores con caracteres especiales, usar
                # urllib.parse.urlencode, pero entonces hay que asegurarse de que
                # la codificación se usa en ambas cosas (firma y URL enviada).
                query_string = "&".join(f"{k}={v}" for k, v in sorted(todos.items()))
                query_string = "?" + query_string
            extra = query_string
        else:
            todos_cuerpo = {**self._product_params, **(body or {})}
            if todos_cuerpo:
                cuerpo_str = json.dumps(todos_cuerpo, separators=(",", ":"))
            extra = cuerpo_str

        # paramsStr = timestamp + METODO + ruta + extra
        # extra (GET) = "?" + querystring (vacío si no hay parámetros)
        # extra (POST) = el cuerpo JSON serializado (vacío si no hay cuerpo)
        params_str = timestamp + metodo_mayusculas + path + extra

        # firma = base64(HMAC-SHA256(secreto, paramsStr))
        firma = base64.b64encode(
            hmac.new(
                self._api_secret.encode(),
                params_str.encode(),
                hashlib.sha256,
            ).digest()
        ).decode()

        # Construir la URL final con la cadena de consulta ya formada (solo GET)
        url = f"{BASE_URL}{path}{query_string}"

        headers = {
            "ACCESS-KEY": self._api_key,
            "ACCESS-SIGN": firma,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": self._passphrase,
            "Content-Type": "application/json",
        }

        # NO pasar params= en GET (la URL ya contiene la query string) ni
        # json= en POST (volvería a serializar y podría no coincidir con lo
        # firmado). El cuerpo POST va con content=, exactamente la cadena
        # que se firmó, codificada a bytes.
        kwargs: dict = {"headers": headers}
        if metodo_mayusculas != "GET":
            kwargs["content"] = cuerpo_str.encode()

        resp = await self._client.request(
            metodo_mayusculas,
            url,
            **kwargs,
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

    async def get_configuracion_symbol(self, symbol: str) -> ConfiguracionCuentaSymbol:
        """Consulta la configuración de margen/apalancamiento de UN símbolo.
        Es una lectura pura: este método, como el resto del cliente, nunca
        cambia nada en la cuenta -eso es responsabilidad exclusiva de un
        humano en el propio Bitget.

        Ver el docstring de `ConfiguracionCuentaSymbol` para el supuesto sin
        verificar sobre el endpoint y los nombres de campo; se aíslan aquí,
        en un único punto de traducción, a propósito.
        """
        payload = await self._pedir(
            "GET", "/api/v2/mix/account/account",
            params={"symbol": symbol, "marginCoin": "USDT"},
        )
        data = payload.get("data", {})
        return ConfiguracionCuentaSymbol(
            margen_aislado=(data.get("marginMode") == "isolated"),
            apalancamiento_long=float(data.get("isolatedLongLever", 0)),
            apalancamiento_short=float(data.get("isolatedShortLever", 0)),
        )

    @staticmethod
    def _hold_side_desde_lado(lado: str) -> str:
        """Traduce el lado de una orden de cierre/stop ("buy"/"sell") al lado
        de la POSICIÓN que cierra ("long"/"short"), que es lo que exige el
        endpoint de plan orders (place-tpsl-order y afines) vía `holdSide`.

        Una orden de venta reduce-only cierra un LONG; una de compra
        reduce-only cierra un SHORT. Es la misma relación que usa
        `BitgetBroker` para decidir el lado de la orden de cierre a partir
        de `Direction`, solo que en sentido inverso.

        SUPUESTO SIN VERIFICAR (ver informe de la tarea): que
        `place-tpsl-order` identifica el lado por `holdSide` y no por
        `side`. Pendiente de confirmar contra la cuenta de simulación.
        """
        if lado == "sell":
            return "long"
        if lado == "buy":
            return "short"
        raise ValueError(f"lado desconocido: {lado!r} (se esperaba 'buy' o 'sell')")

    async def colocar_orden(
        self, symbol: str, lado: str, cantidad: float, reduce_only: bool, client_oid: str,
    ) -> str:
        """Coloca una orden a mercado. Devuelve el `orderId` que asigna Bitget,
        necesario para consultar después el fill real con `get_fill`.

        `lado` ya viene traducido por `BitgetBroker` a vocabulario de Bitget
        ("buy"/"sell"); este cliente no conoce `Direction`.
        """
        cuerpo = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "size": _formato_decimal(cantidad),
            "side": lado,
            "orderType": "market",
            "reduceOnly": "YES" if reduce_only else "NO",
            "clientOid": client_oid,
        }
        payload = await self._pedir("POST", "/api/v2/mix/order/place-order", body=cuerpo)
        return payload.get("data", {}).get("orderId", "")

    async def colocar_stop(
        self, symbol: str, lado: str, cantidad: float, precio_disparo: float, client_oid: str,
    ) -> str:
        """Coloca un stop (plan order) reduce-only. Devuelve el identificador
        del plan order (`orderId`) que Bitget asigna, que es el `stop_id` que
        maneja el resto del bot.

        SUPUESTO SIN VERIFICAR: los campos exactos de `place-tpsl-order`
        (`planType`, `triggerType`, `holdSide`) se toman de la documentación
        general de la API V2 de Bitget, no de una llamada real -el esquema
        de firma sí se verificó (Task 2), pero el cuerpo de este endpoint
        concreto no-. Se marca `planType="loss_plan"` porque este bot solo
        coloca stops de pérdida, nunca de beneficio. Pendiente de confirmar
        contra la cuenta de simulación (Task 12).
        """
        cuerpo = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "planType": "loss_plan",
            "triggerPrice": _formato_decimal(precio_disparo),
            "triggerType": "mark_price",
            "holdSide": self._hold_side_desde_lado(lado),
            "size": _formato_decimal(cantidad),
            "clientOid": client_oid,
            # Redundante con que place-tpsl-order ya cierra posición por
            # holdSide (nunca abre), pero se manda explícito por si la API lo
            # exige o lo usa para validar; no debería tener efecto si no.
            "reduceOnly": "YES",
        }
        payload = await self._pedir("POST", "/api/v2/mix/order/place-tpsl-order", body=cuerpo)
        return payload.get("data", {}).get("orderId", "")

    async def mover_stop(self, symbol: str, stop_id: str, precio_disparo: float) -> str:
        """Modifica el precio de disparo de un stop vivo. Devuelve el
        `orderId` del stop tras la modificación.

        SUPUESTO SIN VERIFICAR: que `modify-tpsl-order` conserva el mismo
        `orderId` tras modificar el precio (a diferencia del `PaperBroker`,
        donde mover = cancelar + recolocar y el id cambia). Si Bitget
        devolviera un `orderId` distinto en `data`, se usa ese; si no viene
        en la respuesta, se conserva el `stop_id` recibido.
        """
        cuerpo = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "orderId": stop_id,
            "triggerPrice": _formato_decimal(precio_disparo),
        }
        payload = await self._pedir("POST", "/api/v2/mix/order/modify-tpsl-order", body=cuerpo)
        return payload.get("data", {}).get("orderId") or stop_id

    async def cancelar_stop(self, symbol: str, stop_id: str) -> None:
        """Cancela un plan order. No es idempotente a este nivel -si Bitget
        rechaza la cancelación porque el stop ya no existe, `_pedir` lanza
        `RuntimeError` igual que ante cualquier otro `code` de error-.

        La idempotencia ("no lanza si el stop ya se ejecutó") es
        responsabilidad de `BitgetBroker`, que es quien conoce la semántica
        de negocio del `Protocol`; este cliente se limita a reportar lo que
        Bitget responde, fielmente y sin interpretarlo.
        """
        cuerpo = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "orderId": stop_id,
            "planType": "loss_plan",
        }
        await self._pedir("POST", "/api/v2/mix/order/cancel-plan-order", body=cuerpo)

    async def get_fill(self, symbol: str, order_id: str) -> FillOrden:
        """Consulta los fills reales de una orden y los agrega en un único
        precio medio (ponderado por cantidad) y una comisión total.

        Una orden a mercado puede fragmentarse en varios fills parciales a
        precios ligeramente distintos; el bot necesita UN precio y UNA
        cantidad para construir la `OrdenEjecutada`, así que se agregan aquí
        en vez de dejar que `BitgetBroker` conozca la forma de la respuesta.

        SUPUESTO SIN VERIFICAR: la forma de la respuesta (`data.fillList`,
        con `price`, `baseVolume` y `feeDetail[].totalFee` por fill) se toma
        de la documentación general de la API V2 de Bitget para
        `/api/v2/mix/order/fills`, no de una llamada real. Pendiente de
        confirmar contra la cuenta de simulación (Task 12).
        """
        payload = await self._pedir(
            "GET", "/api/v2/mix/order/fills",
            params={"symbol": symbol, "orderId": order_id},
        )
        data = payload.get("data", {})
        lista = data.get("fillList", []) if isinstance(data, dict) else data

        cantidad_total = 0.0
        valor_total = 0.0
        comision_total = 0.0
        for item in lista:
            cantidad = float(item.get("baseVolume", 0))
            precio = float(item.get("price", 0))
            cantidad_total += cantidad
            valor_total += cantidad * precio
            for fee in item.get("feeDetail", []) or []:
                comision_total += abs(float(fee.get("totalFee", 0)))

        if cantidad_total <= 0:
            raise RuntimeError(
                f"No se encontraron fills para la orden {order_id!r} en {symbol!r}"
            )

        return FillOrden(
            precio=valor_total / cantidad_total,
            cantidad=cantidad_total,
            comision=comision_total,
        )
