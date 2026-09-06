import base64
import hashlib
import hmac

import pytest

from scanner_volumen.bitget.private import BitgetPrivate


class RespuestaFalsa:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class ClienteFalso:
    """Registra las peticiones para poder inspeccionar cabeceras y cuerpo."""

    def __init__(self, payload):
        self.payload = payload
        self.peticiones = []

    async def request(self, method, url, **kwargs):
        self.peticiones.append({"method": method, "url": url, **kwargs})
        return RespuestaFalsa(self.payload)


def _privado(payload):
    cliente = ClienteFalso(payload)
    return BitgetPrivate("USDT-FUTURES", 10.0, cliente,
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
