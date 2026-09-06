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
    priv, cliente = _privado({"code": "00000", "data": []})
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
