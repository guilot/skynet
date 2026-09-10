import base64
import hashlib
import hmac
import json

import httpx
import pytest

from scanner_volumen.bitget.private import BitgetPrivate


class RespuestaFalsa:
    """Respuesta HTTP falsa.

    `status_code` es configurable a proposito: la version anterior devolvia
    SIEMPRE 200 con un `raise_for_status` que nunca lanzaba, y eso hacia
    invisible todo el manejo de errores HTTP -una revision comprobo que
    revertir el orden de `raise_for_status`/`json` dejaba la suite en verde."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if self._payload is _SIN_JSON:
            raise ValueError("no es JSON")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None)


_SIN_JSON = object()


class ClienteFalso:
    """Registra las peticiones para poder inspeccionar cabeceras y cuerpo.

    `payloads` admite una lista para respuestas distintas en llamadas
    sucesivas (p. ej. leer posiciones y luego escribir configuracion)."""

    def __init__(self, payload, status_code=200):
        self.payload = payload
        self._status = status_code
        self.peticiones = []

    async def request(self, method, url, **kwargs):
        self.peticiones.append({"method": method, "url": url, **kwargs})
        payload = self.payload
        if isinstance(payload, list):
            payload = payload[min(len(self.peticiones) - 1, len(payload) - 1)]
        return RespuestaFalsa(payload, self._status)


def _privado(payload, venue="USDT-FUTURES", status_code=200):
    """`venue` es un parametro para poder ejercitar la moneda de margen
    DERIVADA: con "USDT-FUTURES" coincide con la constante que habia escrita
    a mano, asi que un test que solo use ese venue no distingue el codigo
    corregido del roto -es exactamente lo que paso."""
    cliente = ClienteFalso(payload, status_code)
    return BitgetPrivate(venue, 10.0, cliente,
                         api_key="clave", api_secret="secreto",
                         passphrase="frase"), cliente


async def test_la_peticion_lleva_las_cuatro_cabeceras_de_autenticacion():
    priv, cliente = _privado({"code": "00000", "data": []})
    await priv.get_posiciones()
    cabeceras = cliente.peticiones[0]["headers"]
    for nombre in ("ACCESS-KEY", "ACCESS-SIGN", "ACCESS-TIMESTAMP",
                   "ACCESS-PASSPHRASE"):
        assert nombre in cabeceras, f"falta la cabecera {nombre}"
    assert cabeceras["ACCESS-KEY"] == "clave"


async def test_la_firma_no_es_el_secreto_en_claro():
    # garantia minima de que se firma algo: el secreto nunca viaja tal cual
    priv, cliente = _privado({"code": "00000", "data": []})
    await priv.get_posiciones()
    cabeceras = cliente.peticiones[0]["headers"]
    assert cabeceras["ACCESS-SIGN"] != "secreto"
    assert "secreto" not in str(cabeceras["ACCESS-SIGN"])


async def test_firmas_distintas_para_peticiones_distintas():
    priv, cliente = _privado({"code": "00000", "data": [{
        "marginCoin": "USDT", "accountEquity": "1000.0",
        "unrealizedPL": "0.0", "available": "1000.0",
    }]})
    await priv.get_posiciones()
    await priv.get_saldo()
    firmas = [p["headers"]["ACCESS-SIGN"] for p in cliente.peticiones]
    assert firmas[0] != firmas[1]


async def test_un_code_de_error_de_bitget_se_convierte_en_excepcion():
    priv, _ = _privado({"code": "40001", "msg": "clave invalida"})
    with pytest.raises(RuntimeError, match="40001"):
        await priv.get_posiciones()


async def test_el_mensaje_de_error_no_filtra_las_claves():
    # si Bitget rechaza la firma, el error NO puede llevar la clave ni el secreto
    priv, _ = _privado({"code": "40009", "msg": "firma invalida"})
    with pytest.raises(RuntimeError) as exc:
        await priv.get_saldo()
    texto = str(exc.value)
    assert "secreto" not in texto and "clave" not in texto and "frase" not in texto


async def test_el_saldo_usa_el_equity_menos_el_pnl_no_realizado():
    # ver spec 9: es la unica cifra que replica la semantica del backtest
    priv, _ = _privado({"code": "00000", "data": [{
        "marginCoin": "USDT", "accountEquity": "1050.0",
        "unrealizedPL": "50.0", "available": "800.0",
    }]})
    saldo = await priv.get_saldo()
    assert saldo.realizado == pytest.approx(1000.0)


async def test_las_posiciones_se_mapean_con_simbolo_lado_y_tamano():
    priv, _ = _privado({"code": "00000", "data": [{
        "symbol": "BTCUSDT", "holdSide": "long", "total": "0.5",
        "openPriceAvg": "60000.0",
    }]})
    posiciones = await priv.get_posiciones()
    assert len(posiciones) == 1
    assert posiciones[0].symbol == "BTCUSDT"
    assert posiciones[0].tamano == pytest.approx(0.5)


async def test_repr_no_filtra_las_credenciales():
    """El repr de la instancia nunca debe exponer api_key, api_secret o passphrase."""
    priv, _ = _privado({"code": "00000", "data": []})
    repr_str = repr(priv)
    assert "clave" not in repr_str
    assert "secreto" not in repr_str
    assert "frase" not in repr_str
    # Sí debe incluir el venue
    assert "USDT-FUTURES" in repr_str


async def test_la_cadena_de_consulta_firmada_es_exactamente_la_que_se_envia():
    """Verifica que lo que se firma es byte a byte lo que se envía.

    Este test cierra el agujero donde la firma se construye con una serialización
    de parámetros pero la URL se construye con otra, haciendo que Bitget rechace
    la firma con error genérico.
    """
    priv, cliente = _privado({"code": "00000", "data": [{
        "marginCoin": "USDT", "accountEquity": "1000.0",
        "unrealizedPL": "0.0", "available": "1000.0",
    }]})
    await priv.get_saldo()

    peticion = cliente.peticiones[0]
    url_enviada = peticion["url"]
    headers = peticion["headers"]
    timestamp = headers["ACCESS-TIMESTAMP"]
    firma_recibida = headers["ACCESS-SIGN"]

    # Extraer path y query string de la URL
    # URL tiene formato: https://api.bitget.com/ruta?parámetros
    # Queremos extraer /ruta?parámetros o /ruta (sin ?)
    assert url_enviada.startswith("https://api.bitget.com")
    path_y_query = url_enviada[len("https://api.bitget.com"):]

    # Separar path de query string
    if "?" in path_y_query:
        path, query_parte = path_y_query.split("?", 1)
        query_string = "?" + query_parte
    else:
        path = path_y_query
        query_string = ""

    # Reconstruir la firma con lo que se envió
    params_str = timestamp + "GET" + path + query_string
    firma_esperada = base64.b64encode(
        hmac.new("secreto".encode(), params_str.encode(), hashlib.sha256).digest()
    ).decode()

    # La firma debe coincidir exactamente
    assert firma_recibida == firma_esperada, (
        f"La firma enviada no coincide con la esperada. "
        f"URL={url_enviada}, params_str={params_str!r}"
    )


async def test_el_saldo_selecciona_usdt_cuando_hay_multiples_monedas():
    """Cuando hay múltiples monedas de margen, se escoge explícitamente USDT."""
    priv, _ = _privado({"code": "00000", "data": [
        {
            "marginCoin": "BTC", "accountEquity": "1.0",
            "unrealizedPL": "0.1", "available": "0.5",
        },
        {
            "marginCoin": "USDT", "accountEquity": "1050.0",
            "unrealizedPL": "50.0", "available": "800.0",
        },
        {
            "marginCoin": "ETH", "accountEquity": "10.0",
            "unrealizedPL": "1.0", "available": "5.0",
        },
    ]})
    saldo = await priv.get_saldo()
    # Debe coger la de USDT, no la primera (BTC)
    assert saldo.realizado == pytest.approx(1000.0)
    assert saldo.equity == pytest.approx(1050.0)


async def test_el_saldo_falla_si_no_hay_usdt():
    """Si no hay saldo en USDT, lanza un error claro."""
    priv, _ = _privado({"code": "00000", "data": [
        {
            "marginCoin": "BTC", "accountEquity": "1.0",
            "unrealizedPL": "0.1", "available": "0.5",
        },
        {
            "marginCoin": "ETH", "accountEquity": "10.0",
            "unrealizedPL": "1.0", "available": "5.0",
        },
    ]})
    with pytest.raises(RuntimeError, match="No se encontró saldo en USDT"):
        await priv.get_saldo()


# --- get_saldo falla cerrado ante un campo critico ausente (Task 11, ronda
# de arreglo) -----------------------------------------------------------
#
# Antes de este arreglo, un campo ausente se rellenaba con 0 en silencio:
# con "unrealizedPL" ausente (p.ej. renombrado por la API a algo distinto),
# `realizado` salia identico al equity CON pnl no realizado -exactamente lo
# que el Step 1 de la Task 11 dice que nunca debe pasar ("infla el tamano
# con ganancias que aun no existen")- sin ninguna excepcion que lo delatara.


async def test_el_saldo_falla_si_falta_account_equity():
    priv, _ = _privado({"code": "00000", "data": [{
        "marginCoin": "USDT", "unrealizedPL": "50.0", "available": "800.0",
        # sin "accountEquity"
    }]})
    with pytest.raises(RuntimeError, match="accountEquity"):
        await priv.get_saldo()


async def test_el_saldo_falla_si_falta_unrealized_pl():
    # Reproduce el caso B de la revision: el campo llega renombrado
    # (p.ej. "unrealisedPL", con "s" britanica) y antes de este arreglo
    # `realizado` habria salido igual al equity CON pnl no realizado.
    priv, _ = _privado({"code": "00000", "data": [{
        "marginCoin": "USDT", "accountEquity": "1000.0",
        "unrealisedPL": "300.0",  # nombre distinto a proposito: no se lee
        "available": "800.0",
    }]})
    with pytest.raises(RuntimeError, match="unrealizedPL"):
        await priv.get_saldo()


async def test_el_saldo_no_falla_si_falta_available():
    # "available" es puramente informativo (no alimenta ningun calculo de
    # margen, ver LivePortfolio.margen): que falte no debe impedir calcular
    # el saldo realizado, a diferencia de los dos campos criticos de arriba.
    priv, _ = _privado({"code": "00000", "data": [{
        "marginCoin": "USDT", "accountEquity": "1000.0", "unrealizedPL": "50.0",
        # sin "available"
    }]})
    saldo = await priv.get_saldo()
    assert saldo.realizado == pytest.approx(950.0)
    assert saldo.disponible == pytest.approx(0.0)


# --- Configuración de cuenta por símbolo (Task 11) -------------------------
#
# Estos tests fijan la TRADUCCIÓN que hace `get_configuracion_symbol`, no la
# forma real de la respuesta de Bitget: los nombres de campo (`marginMode`,
# `isolatedLongLever`, `isolatedShortLever`) son un supuesto sin verificar
# -documentado en `ConfiguracionCuentaSymbol`- pendiente de confirmar contra
# la cuenta de simulación en la Task 12. Si esos nombres resultan ser otros,
# lo que hay que corregir es el payload de estos tests y el punto único de
# traducción en `get_configuracion_symbol`, no la lógica que los consume
# (`VerificadorCuenta`, que ya está probada contra el tipo traducido, sin
# tocar red).


async def test_configuracion_symbol_margen_aislado():
    priv, _ = _privado({"code": "00000", "data": {
        "marginMode": "isolated", "isolatedLongLever": "20",
        "isolatedShortLever": "20",
    }})
    config = await priv.get_configuracion_symbol("BTCUSDT")
    assert config.margen_aislado is True
    assert config.apalancamiento_long == pytest.approx(20.0)
    assert config.apalancamiento_short == pytest.approx(20.0)


async def test_configuracion_symbol_margen_cruzado():
    priv, _ = _privado({"code": "00000", "data": {
        "marginMode": "crossed", "isolatedLongLever": "20",
        "isolatedShortLever": "20",
    }})
    config = await priv.get_configuracion_symbol("BTCUSDT")
    assert config.margen_aislado is False


async def test_configuracion_symbol_apalancamiento_distinto_por_lado():
    # Bitget permite apalancamiento distinto para long y short en margen
    # aislado: la traducción debe conservar los dos valores por separado,
    # no colapsarlos en uno solo.
    priv, _ = _privado({"code": "00000", "data": {
        "marginMode": "isolated", "isolatedLongLever": "20",
        "isolatedShortLever": "10",
    }})
    config = await priv.get_configuracion_symbol("BTCUSDT")
    assert config.apalancamiento_long == pytest.approx(20.0)
    assert config.apalancamiento_short == pytest.approx(10.0)


async def test_configuracion_symbol_manda_symbol_y_margin_coin():
    priv, cliente = _privado({"code": "00000", "data": {
        "marginMode": "isolated", "isolatedLongLever": "20",
        "isolatedShortLever": "20",
    }})
    await priv.get_configuracion_symbol("BTCUSDT")
    url_enviada = cliente.peticiones[0]["url"]
    assert "symbol=BTCUSDT" in url_enviada
    assert "marginCoin=USDT" in url_enviada


# --- Escrituras (Task 6) --------------------------------------------------


async def test_el_cuerpo_post_firmado_es_exactamente_el_que_se_envia():
    """Cierra para POST el mismo agujero que el test de GET cierra para la
    query string: el cuerpo que se firma debe ser byte a byte el que
    viaja en `content=`. Si alguna vez se colara un `json=` (que
    reserializaría) esto lo detectaría, porque reconstruye la firma a
    partir del `content` realmente enviado y la compara con `ACCESS-SIGN`.
    """
    priv, cliente = _privado({"code": "00000", "data": {"orderId": "111"}})
    await priv.colocar_orden(
        symbol="BTCUSDT", lado="buy", cantidad=1.5,
        reduce_only=False, client_oid="oid-1",
    )

    peticion = cliente.peticiones[0]
    assert peticion["method"] == "POST"
    cuerpo_enviado = peticion["content"].decode()
    headers = peticion["headers"]
    timestamp = headers["ACCESS-TIMESTAMP"]
    firma_recibida = headers["ACCESS-SIGN"]

    path = peticion["url"][len("https://api.bitget.com"):]
    params_str = timestamp + "POST" + path + cuerpo_enviado
    firma_esperada = base64.b64encode(
        hmac.new("secreto".encode(), params_str.encode(), hashlib.sha256).digest()
    ).decode()

    assert firma_recibida == firma_esperada


async def test_colocar_orden_manda_side_size_y_client_oid_reduce_only_false():
    priv, cliente = _privado({"code": "00000", "data": {"orderId": "111"}})
    await priv.colocar_orden(
        symbol="BTCUSDT", lado="buy", cantidad=1.5,
        reduce_only=False, client_oid="oid-abrir",
    )
    cuerpo = json.loads(cliente.peticiones[0]["content"])
    assert cuerpo["symbol"] == "BTCUSDT"
    assert cuerpo["side"] == "buy"
    assert cuerpo["size"] == "1.5"
    assert cuerpo["clientOid"] == "oid-abrir"
    assert cuerpo["reduceOnly"] == "NO"


async def test_colocar_orden_devuelve_el_order_id():
    priv, _ = _privado({"code": "00000", "data": {"orderId": "abc-123"}})
    order_id = await priv.colocar_orden(
        symbol="BTCUSDT", lado="sell", cantidad=2.0,
        reduce_only=True, client_oid="oid-cerrar",
    )
    assert order_id == "abc-123"


async def test_colocar_orden_de_cierre_va_reduce_only():
    priv, cliente = _privado({"code": "00000", "data": {"orderId": "111"}})
    await priv.colocar_orden(
        symbol="BTCUSDT", lado="sell", cantidad=2.0,
        reduce_only=True, client_oid="oid-cerrar",
    )
    cuerpo = json.loads(cliente.peticiones[0]["content"])
    assert cuerpo["reduceOnly"] == "YES"


async def test_colocar_stop_siempre_va_reduce_only_y_traduce_el_hold_side():
    """Un stop que cierra un LONG manda lado 'sell'; BitgetPrivate lo traduce
    a holdSide='buy' para place-tpsl-order y marca reduceOnly='YES'.

    OJO con el vocabulario, que es contraintuitivo y costo una ronda:
    `holdSide` NO es "long"/"short" sino "buy"/"sell", donde "buy" es la
    posicion LARGA. Mandar "long" hace que Bitget rechace con code=43011
    (observado contra la simulacion). Y no es un simple error de validacion:
    mandar el contrario coloca el stop sobre el otro lado -sobre una
    posicion larga, holdSide="sell" devuelve code=45122 hablando de la
    posicion CORTA."""
    priv, cliente = _privado({"code": "00000", "data": {"orderId": "stop-1"}})
    stop_id = await priv.colocar_stop(
        symbol="BTCUSDT", lado="sell", cantidad=4.0,
        precio_disparo=97.5, client_oid="oid-stop",
    )
    assert stop_id == "stop-1"
    cuerpo = json.loads(cliente.peticiones[0]["content"])
    assert cuerpo["holdSide"] == "buy"
    assert cuerpo["reduceOnly"] == "YES"
    assert cuerpo["triggerPrice"] == "97.5"


async def test_colocar_stop_de_un_short_traduce_hold_side_sell():
    priv, cliente = _privado({"code": "00000", "data": {"orderId": "stop-2"}})
    await priv.colocar_stop(
        symbol="BTCUSDT", lado="buy", cantidad=4.0,
        precio_disparo=105.0, client_oid="oid-stop-2",
    )
    cuerpo = json.loads(cliente.peticiones[0]["content"])
    # cerrar un SHORT es comprar -> la posicion se identifica como "sell"
    assert cuerpo["holdSide"] == "sell"


async def test_mover_stop_manda_el_order_id_y_el_nuevo_precio():
    priv, cliente = _privado({"code": "00000", "data": {"orderId": "stop-1"}})
    nuevo_id = await priv.mover_stop(symbol="BTCUSDT", stop_id="stop-1", precio_disparo=99.0,
                                    cantidad=4.0)
    assert nuevo_id == "stop-1"
    cuerpo = json.loads(cliente.peticiones[0]["content"])
    assert cuerpo["orderId"] == "stop-1"
    assert cuerpo["triggerPrice"] == "99"


async def test_mover_stop_sin_order_id_en_la_respuesta_conserva_el_recibido():
    priv, _ = _privado({"code": "00000", "data": {}})
    nuevo_id = await priv.mover_stop(symbol="BTCUSDT", stop_id="stop-1", precio_disparo=99.0,
                                    cantidad=4.0)
    assert nuevo_id == "stop-1"


async def test_cancelar_stop_lanza_si_bitget_devuelve_error():
    priv, _ = _privado({"code": "40768", "msg": "order does not exist"})
    with pytest.raises(RuntimeError, match="40768"):
        await priv.cancelar_stop(symbol="BTCUSDT", stop_id="ya-no-existe")


async def test_get_fill_agrega_varios_fills_parciales_en_precio_medio_ponderado():
    priv, _ = _privado({"code": "00000", "data": {"fillList": [
        {"price": "100.0", "baseVolume": "1.0", "feeDetail": [{"totalFee": "-0.06"}]},
        {"price": "102.0", "baseVolume": "3.0", "feeDetail": [{"totalFee": "-0.18"}]},
    ]}})
    fill = await priv.get_fill("BTCUSDT", "order-1")
    # (100*1 + 102*3) / 4 = 101.5
    assert fill.precio == pytest.approx(101.5)
    assert fill.cantidad == pytest.approx(4.0)
    assert fill.comision == pytest.approx(0.24)


async def test_get_fill_sin_fills_lanza():
    priv, _ = _privado({"code": "00000", "data": {"fillList": []}})
    with pytest.raises(RuntimeError, match="No se encontraron fills"):
        await priv.get_fill("BTCUSDT", "order-inexistente")


# --- los defectos que el banco de pruebas encontro, y que nada protegia ---
#
# Una revision midio que 11 de 24 mutaciones sobre este bloque pasaban en
# VERDE, incluidos los seis defectos que el bloque existe para arreglar. La
# causa era el andamiaje: todos los tests construian con "USDT-FUTURES" (donde
# la moneda derivada coincide con la constante vieja) y la respuesta falsa
# nunca lanzaba en `raise_for_status`.


async def test_la_moneda_de_margen_se_deriva_del_producttype():
    """`marginCoin` NO es siempre "USDT": con `SUSDT-FUTURES` es `SUSDT`, y
    mandar la equivocada hace que Bitget rechace con code=40778 (observado).
    Estaba escrita a mano en seis sitios."""
    priv, cliente = _privado({"code": "00000", "data": {"orderId": "1"}},
                             venue="SUSDT-FUTURES")
    await priv.colocar_orden(symbol="SBTCSUSDT", lado="buy", cantidad=1.0,
                             reduce_only=False, client_oid="x")
    cuerpo = json.loads(cliente.peticiones[0]["content"])
    assert cuerpo["marginCoin"] == "SUSDT"


async def test_el_saldo_selecciona_la_cuenta_de_la_moneda_derivada():
    """El mismo defecto por el lado de la lectura: con `SUSDT-FUTURES` la
    cuenta que hay que elegir es la de `SUSDT`, no la de `USDT`."""
    priv, _ = _privado(
        {"code": "00000", "data": [
            {"marginCoin": "USDT", "accountEquity": "999", "unrealizedPL": "0"},
            {"marginCoin": "SUSDT", "accountEquity": "3000", "unrealizedPL": "0"},
        ]},
        venue="SUSDT-FUTURES")
    saldo = await priv.get_saldo()
    assert saldo.equity == pytest.approx(3000.0)


async def test_place_order_manda_el_modo_de_margen():
    """Sin `marginMode`, Bitget rechaza con code=400172 "The margin mode
    cannot be empty": en modo real TODAS las ordenes habrian sido
    rechazadas."""
    priv, cliente = _privado({"code": "00000", "data": {"orderId": "1"}})
    await priv.colocar_orden(symbol="BTCUSDT", lado="buy", cantidad=1.0,
                             reduce_only=False, client_oid="x")
    cuerpo = json.loads(cliente.peticiones[0]["content"])
    assert cuerpo["marginMode"] == "isolated"


async def test_modify_tpsl_manda_cantidad_y_plantype():
    """Sin `size`, code=400172 "Order quantity cannot be empty" aunque solo
    se cambie el precio."""
    priv, cliente = _privado({"code": "00000", "data": {"orderId": "s1"}})
    await priv.mover_stop(symbol="BTCUSDT", stop_id="s1", precio_disparo=99.0,
                          cantidad=2.5)
    cuerpo = json.loads(cliente.peticiones[0]["content"])
    assert cuerpo["size"] == "2.5"
    assert cuerpo["planType"] == "loss_plan"


async def test_el_modo_de_posicion_se_lee_y_falla_cerrado():
    """`modo_una_via` decide si existe `reduceOnly`, sobre el que descansa
    toda la seguridad de la fase. Cualquier valor que no sea exactamente
    "one_way_mode" -incluido el campo ausente- se lee como NO unilateral."""
    for pos_mode, esperado in (("one_way_mode", True), ("hedge_mode", False),
                               (None, False), ("", False)):
        datos = {"marginMode": "isolated", "isolatedLongLever": 20,
                 "isolatedShortLever": 20}
        if pos_mode is not None:
            datos["posMode"] = pos_mode
        priv, _ = _privado({"code": "00000", "data": datos})
        cfg = await priv.get_configuracion_symbol("BTCUSDT")
        assert cfg.modo_una_via is esperado, f"posMode={pos_mode!r}"


async def test_un_error_http_conserva_el_diagnostico_de_bitget():
    """El defecto: `raise_for_status()` corria ANTES de leer el JSON, asi que
    un 400 llegaba como `HTTPStatusError: 400 Bad Request` y se perdia el
    `code`/`msg` con el que Bitget explica el problema -lo unico que
    distingue "mande mal un parametro" de "la firma esta rota"."""
    priv, _ = _privado(
        {"code": "40778", "msg": "SBTCSUSDT does not support USDT currency as margin"},
        status_code=400)
    with pytest.raises(RuntimeError) as exc:
        await priv.get_saldo()
    assert "40778" in str(exc.value)
    assert "does not support USDT currency" in str(exc.value)


async def test_una_respuesta_que_no_es_json_sigue_propagando_el_error_http():
    """La contrapartida: leer el cuerpo primero no puede tragarse un fallo
    HTTP cuando NO hay JSON que interpretar (un 502 de un proxy, p. ej.)."""
    priv, _ = _privado(_SIN_JSON, status_code=502)
    with pytest.raises(httpx.HTTPStatusError):
        await priv.get_saldo()


async def test_ajustar_la_configuracion_manda_aislado_y_el_apalancamiento():
    """La UNICA escritura de este cliente que no es una orden, y no tenia ni
    un test."""
    priv, cliente = _privado([
        {"code": "00000", "data": []},                      # get_posiciones
        {"code": "00000", "data": {}},                      # set-margin-mode
        {"code": "00000", "data": {}},                      # set-leverage
    ])
    await priv.ajustar_configuracion_symbol("BTCUSDT", 20.0)

    escrituras = [p for p in cliente.peticiones if p["method"] == "POST"]
    cuerpos = [json.loads(p["content"]) for p in escrituras]
    assert any("set-margin-mode" in p["url"] for p in escrituras)
    assert cuerpos[0]["marginMode"] == "isolated"
    assert any("set-leverage" in p["url"] for p in escrituras)
    assert cuerpos[1]["leverage"] == "20"


async def test_no_se_ajusta_la_configuracion_de_un_simbolo_con_posicion_abierta():
    """La garantia estaba solo en los comentarios y nada la imponia: una
    posicion que el usuario abra a mano DESPUES del arranque no esta ni en
    `abiertas` ni entre los vetados, y se le habria reescrito el margen y el
    apalancamiento -moviendole el precio de liquidacion."""
    priv, cliente = _privado(
        {"code": "00000", "data": [
            {"symbol": "BTCUSDT", "holdSide": "long", "total": "1.0",
             "openPriceAvg": "100"},
        ]})
    with pytest.raises(RuntimeError, match="posicion abierta"):
        await priv.ajustar_configuracion_symbol("BTCUSDT", 20.0)

    assert [p for p in cliente.peticiones if p["method"] == "POST"] == []
