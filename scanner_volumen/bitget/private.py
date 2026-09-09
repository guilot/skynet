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

    CONFIRMADO contra la cuenta de simulación (Task 12, ejecutado): el
    endpoint es `GET /api/v2/mix/account/account` (singular, con `symbol` +
    `marginCoin` + `productType`), y devuelve `marginMode` con valores
    "isolated"/"crossed", `isolatedLongLever` e `isolatedShortLever` (como
    enteros, no cadenas) y `posMode` con valores "one_way_mode"/"hedge_mode".

    **`modo_una_via` no es un detalle de configuración: es la condición sin
    la cual la garantía central de esta fase no existe.** Todo cierre y todo
    stop que manda este bot son `reduceOnly`, que es lo que impide que una
    orden de cierre pueda ABRIR una posición contraria por error. Y
    `reduceOnly` solo existe en modo de posición unilateral: en `hedge_mode`
    Bitget rechaza la orden con `code=40774 "The order type for unilateral
    position must also be the unilateral position type."` (observado, no
    supuesto). O sea que en `hedge_mode` el bot no podría cerrar nada, y la
    alternativa que Bitget ofrece ahí -`tradeSide: "close"`- NO tiene la
    propiedad de reduce-only que la seguridad de esta fase asume.
    """
    margen_aislado: bool
    apalancamiento_long: float
    apalancamiento_short: float
    modo_una_via: bool


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
    # Instante del fill segun el exchange (`cTime`), en ms. Por defecto 0
    # cuando quien construye esto no lo tiene a mano; nadie lo lee hoy, pero
    # llevarlo cuando SI se conoce evita que alguien lo rellene mas adelante
    # con la hora local creyendo que es la del exchange.
    ts: int = 0


def _formato_decimal(valor: float) -> str:
    """Formatea un número para el cuerpo de una petición a Bitget.

    Bitget espera los campos numéricos (tamaños, precios) como cadenas
    decimales, no en notación científica. `repr`/`str` de un float puede caer
    en notación científica para valores muy pequeños o muy grandes; este
    formato evita eso y recorta ceros sobrantes.
    """
    formateado = f"{valor:.10f}".rstrip("0").rstrip(".")
    return formateado if formateado else "0"


# El modo de margen que la estrategia asume y que `VerificacionCuenta` exige
# por símbolo antes de la primera entrada: si el símbolo no está en aislado,
# el bot VETA en vez de operarlo (y nunca cambia la configuración de la
# cuenta). Mandarlo en la orden es obligatorio -Bitget rechaza `place-order`
# sin él con `code=400172 "The margin mode cannot be empty"` (observado
# contra la cuenta de simulación)- y el valor coherente es exactamente el
# que se verifica: pedir "crossed" aquí abriría en un modo que la estrategia
# no ha dimensionado.
MODO_MARGEN = "isolated"

# El tipo de plan order de los stops de pérdida. CONFIRMADO contra la
# simulación: `place-tpsl-order` y `modify-tpsl-order` lo aceptan con este
# valor. Este bot solo coloca stops de pérdida, nunca de beneficio.
PLAN_TYPE_STOP = "loss_plan"


def _moneda_de_margen(venue: str) -> str:
    """La moneda de margen que corresponde a un `productType`.

    NO es siempre `"USDT"`, y darlo por hecho fue un defecto real que solo
    apareció al ejecutar el banco de pruebas contra la cuenta de simulación:
    con `productType = "SUSDT-FUTURES"` la moneda es `SUSDT`, y mandar
    `USDT` hace que Bitget rechace la petición con
    `code=40778 "SBTCSUSDT does not support USDT currency as margin"`
    (observado, no supuesto). Antes de esto la moneda estaba escrita a mano
    en seis sitios de este fichero, lo que ataba el cliente a un único
    entorno sin que nada lo dijera.

    La regla es el propio `productType` sin su sufijo: `USDT-FUTURES` ->
    `USDT`, `SUSDT-FUTURES` -> `SUSDT`, `USDC-FUTURES` -> `USDC`.
    """
    sin_sufijo = venue.removesuffix("-FUTURES")
    if not sin_sufijo or sin_sufijo == venue:
        raise ValueError(
            f"productType inesperado: {venue!r} (se esperaba algo como "
            f"'USDT-FUTURES' o 'SUSDT-FUTURES'). No se adivina la moneda de "
            f"margen: mandar la equivocada hace que Bitget rechace todas las "
            f"peticiones de esta cuenta."
        )
    return sin_sufijo


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
        self._margin_coin = _moneda_de_margen(venue)

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
        # El cuerpo se lee ANTES de mirar el estado HTTP, a propósito. Bitget
        # manda su diagnóstico útil DENTRO del JSON incluso cuando responde
        # 4xx: el defecto que motiva esto se vio ejecutando el banco contra la
        # simulación, donde un `raise_for_status()` previo convertía
        # `{"code":"40778","msg":"SBTCSUSDT does not support USDT currency as
        # margin"}` -que nombra el problema exacto- en un `HTTPStatusError:
        # 400 Bad Request` sin ninguna pista. Perder ese mensaje es perder lo
        # único que distingue "mandé mal un parámetro" de "la firma está rota",
        # y esa confusión ya costó una ronda entera de depuración.
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            resp.raise_for_status()
            raise RuntimeError(
                f"Bitget devolvió una respuesta no interpretable en {path} "
                f"(HTTP {resp.status_code})"
            )

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
        la semántica del backtest -y la que gobierna el margen de cada
        entrada en modo real (Task 11), así que un fallo aquí no puede
        quedar en silencio.

        FALLA CERRADO (hallazgo de revisión, Task 11): `accountEquity` y
        `unrealizedPL` son OBLIGATORIOS en la respuesta -si Bitget cambiara
        alguno de esos dos nombres de campo (ver la lista de supuestos sin
        verificar en el informe de la tarea), la versión anterior de este
        método calculaba con `0` como si el campo faltante valiera cero,
        lo que en el caso de `unrealizedPL` ausente da un `realizado` IGUAL
        al equity CON PnL no realizado incluido -exactamente lo que el Step
        1 de esta tarea existe para evitar (infla el tamaño de posición con
        ganancias que todavía no existen), y sin ninguna excepción ni log
        que lo delatara. `available` se deja con `0` por defecto a
        propósito: es puramente informativo (`SaldoCuenta.disponible` no
        alimenta ningún cálculo de margen, ver el docstring de
        `LivePortfolio.margen`), así que perderlo no es un riesgo de
        dinero.
        """
        payload = await self._pedir("GET", "/api/v2/mix/account/accounts")
        data_list = payload.get("data", [])

        # Seleccionar explícitamente la cuenta en USDT
        data = None
        for item in data_list:
            if item.get("marginCoin") == self._margin_coin:
                data = item
                break

        if data is None:
            raise RuntimeError(
                f"No se encontró saldo en {self._margin_coin} en Bitget"
            )

        if "accountEquity" not in data or "unrealizedPL" not in data:
            # NO se rellena con 0: un campo crítico ausente es una API que
            # cambió de forma, no una cuenta con saldo cero -y calcular con
            # un 0 inventado aquí movería dinero real sobre una cifra falsa.
            raise RuntimeError(
                "Bitget no devolvió 'accountEquity' o 'unrealizedPL' en el "
                "saldo de USDT; no se puede calcular el saldo realizado con "
                "seguridad"
            )

        equity = float(data["accountEquity"])
        pnl_no_realizado = float(data["unrealizedPL"])
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
            params={"symbol": symbol, "marginCoin": self._margin_coin},
        )
        data = payload.get("data", {})
        return ConfiguracionCuentaSymbol(
            margen_aislado=(data.get("marginMode") == "isolated"),
            apalancamiento_long=float(data.get("isolatedLongLever", 0)),
            apalancamiento_short=float(data.get("isolatedShortLever", 0)),
            # Falla cerrado a propósito: cualquier valor que no sea
            # exactamente "one_way_mode" -incluido un campo ausente o un
            # nombre que Bitget cambie- se lee como "no es unilateral" y el
            # símbolo acaba vetado. Equivocarse hacia el veto cuesta no
            # operar; equivocarse hacia el otro lado significa mandar
            # cierres que el exchange rechaza y creer que hay una red que no
            # está.
            modo_una_via=(data.get("posMode") == "one_way_mode"),
        )

    @staticmethod
    def _hold_side_desde_lado(lado: str) -> str:
        """Traduce el lado de una orden de cierre/stop ("buy"/"sell") al
        `holdSide` que identifica la POSICIÓN sobre la que actúa el stop.

        CORREGIDO tras ejecutar el banco contra la simulación: `holdSide` NO
        usa el vocabulario de posición ("long"/"short") sino el de orden
        ("buy"/"sell"), donde **"buy" identifica la posición larga y "sell"
        la corta** -es decir, el lado con el que se ABRIÓ la posición, no el
        de la orden que la cierra-. Mandar "long"/"short" hace que Bitget
        rechace con `code=43011 "The parameter does not meet the
        specification holdSide error"` (observado).

        Que discrimina de verdad también está comprobado: sobre una posición
        LARGA, un `holdSide="sell"` no se ignora, se interpreta como la
        posición CORTA y Bitget contesta `code=45122 "Short position stop
        loss price please > mark price"`. Equivocar este campo no da un
        error de validación: coloca el stop sobre el lado contrario.

        Una orden de venta reduce-only cierra un LONG -> holdSide "buy";
        una de compra reduce-only cierra un SHORT -> holdSide "sell".
        """
        if lado == "sell":
            return "buy"
        if lado == "buy":
            return "sell"
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
            **self._product_params,
            "symbol": symbol,
            "marginCoin": self._margin_coin,
            "marginMode": MODO_MARGEN,
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
            **self._product_params,
            "symbol": symbol,
            "marginCoin": self._margin_coin,
            "planType": PLAN_TYPE_STOP,
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

    async def mover_stop(
        self, symbol: str, stop_id: str, precio_disparo: float, cantidad: float,
    ) -> str:
        """Modifica el precio de disparo de un stop vivo. Devuelve el
        `orderId` del stop tras la modificación.

        CONFIRMADO contra la simulación: `modify-tpsl-order` **conserva el
        mismo `orderId`** tras modificar el precio (a diferencia del
        `PaperBroker`, donde mover = cancelar + recolocar y el id cambia).
        Se sigue leyendo el `orderId` de la respuesta por si algún día
        cambiara; si no viene, se conserva el `stop_id` recibido.
        """
        cuerpo = {
            **self._product_params,
            "symbol": symbol,
            "marginCoin": self._margin_coin,
            "orderId": stop_id,
            "triggerPrice": _formato_decimal(precio_disparo),
            # `size` y `planType` son OBLIGATORIOS aunque solo se cambie el
            # precio: sin `size`, Bitget responde `code=400172 "Order
            # quantity cannot be empty"` (observado contra la simulación).
            # Que haya que remandarlos tiene una ventaja: el stop del
            # exchange queda siempre dimensionado a lo que de verdad sigue
            # abierto, en vez de conservar la cantidad original tras una
            # salida parcial.
            "size": _formato_decimal(cantidad),
            "planType": PLAN_TYPE_STOP,
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
            **self._product_params,
            "symbol": symbol,
            "marginCoin": self._margin_coin,
            "orderId": stop_id,
            "planType": PLAN_TYPE_STOP,
        }
        await self._pedir("POST", "/api/v2/mix/order/cancel-plan-order", body=cuerpo)

    async def get_orden_por_client_oid(
        self, symbol: str, client_oid: str, desde_ms: int,
    ) -> FillOrden | None:
        """Busca en el historial de órdenes la que se mandó con ese
        `clientOid` y devuelve su fill agregado, o `None` si no aparece.

        **Para qué existe.** Cuando el stop salta en el exchange, el bot no
        tiene el `orderId` de la orden que Bitget creó -esa orden la generó
        el exchange, no nosotros-, así que `get_fill` (que exige `orderId`)
        no sirve. Sin esto, la posición se quedaba varada: el bot detectaba
        que ya no está en el exchange pero no podía saber a qué precio se
        cerró, y nunca inventa uno.

        **La correlación es exacta, no una heurística.** Observado contra la
        simulación: cuando un plan order (stop) se ejecuta, la orden
        resultante lleva como `clientOid` el `orderId` DEL PROPIO PLAN ORDER
        -es decir, el `stop_id` que el bot ya tiene guardado-, y su
        `orderSource` es `"loss_market"`. Así que no hay que adivinar cuál
        de los fills recientes era el nuestro por ventana de tiempo: se
        busca por identificador.

        Sirve igual para el otro caso que lo necesitaba: una orden de
        APERTURA mandada por un proceso que murió antes de registrarla. Ahí
        el `clientOid` es el que generó el bot, y `all-position` no lo
        devuelve -por eso la reconciliación no podía reconocer su propia
        posición.

        `desde_ms` acota la ventana del historial; Bitget exige un rango.
        """
        payload = await self._pedir(
            "GET", "/api/v2/mix/order/orders-history",
            params={
                "symbol": symbol,
                "startTime": str(int(desde_ms)),
                "endTime": str(int(time.time() * 1000)),
            },
        )
        data = payload.get("data") or {}
        for orden in data.get("entrustedList") or []:
            if orden.get("clientOid") != client_oid:
                continue
            if orden.get("status") != "filled":
                # existe pero no llegó a ejecutarse: no es un cierre real,
                # y devolver un precio de una orden a medias sería peor que
                # decir que no se encontró.
                return None
            cantidad = float(orden.get("baseVolume") or 0)
            precio = float(orden.get("priceAvg") or 0)
            if cantidad <= 0 or precio <= 0:
                return None
            # `fee` viene NEGATIVO (lo que cobró el exchange); el resto del
            # bot trata la comisión como un coste positivo.
            return FillOrden(
                precio=precio, cantidad=cantidad,
                comision=abs(float(orden.get("fee") or 0)),
                ts=int(orden.get("cTime") or 0),
            )
        return None

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
