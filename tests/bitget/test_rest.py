import json
from pathlib import Path

import httpx

from scanner_volumen.bitget.rest import BitgetRest

FIXTURES = Path(__file__).parent.parent / "fixtures"


def transporte_de_fixtures():
    """Devuelve un transporte httpx que responde con los fixtures guardados,
    y registra las URLs pedidas para poder verificarlas."""
    pedidas: list[httpx.URL] = []

    mapa = {
        "/api/v2/mix/market/contracts": "contracts_usdt_futures.json",
        "/api/v2/mix/market/tickers": "tickers_usdt_futures.json",
        "/api/v2/mix/market/candles": "candles_btc_1m.json",
        "/api/v2/mix/market/history-candles": "history_candles_btc_1m.json",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        pedidas.append(request.url)
        nombre = mapa[request.url.path]
        return httpx.Response(200, json=json.loads((FIXTURES / nombre).read_text()))

    return httpx.MockTransport(handler), pedidas


async def test_get_contracts_devuelve_modelos():
    transporte, _ = transporte_de_fixtures()
    async with httpx.AsyncClient(transport=transporte, base_url="https://api.bitget.com") as cli:
        rest = BitgetRest(venue="USDT-FUTURES", rate_limit=1000, client=cli)
        contratos = await rest.get_contracts()
    assert len(contratos) == 754
    assert any(c.symbol == "BTCUSDT" for c in contratos)


async def test_get_tickers_devuelve_modelos():
    transporte, _ = transporte_de_fixtures()
    async with httpx.AsyncClient(transport=transporte, base_url="https://api.bitget.com") as cli:
        rest = BitgetRest(venue="USDT-FUTURES", rate_limit=1000, client=cli)
        tickers = await rest.get_tickers()
    assert len(tickers) == 754


async def test_get_history_candles_envia_endtime_y_limite_200():
    transporte, pedidas = transporte_de_fixtures()
    async with httpx.AsyncClient(transport=transporte, base_url="https://api.bitget.com") as cli:
        rest = BitgetRest(venue="USDT-FUTURES", rate_limit=1000, client=cli)
        velas = await rest.get_history_candles("BTCUSDT", end_time_ms=1786000000000)
    assert len(velas) == 200
    url = pedidas[-1]
    assert url.params["endTime"] == "1786000000000"
    assert url.params["limit"] == "200"
    assert url.params["granularity"] == "1m"


async def test_limit_por_encima_de_200_se_recorta():
    """history-candles devuelve error 40020 con limit > 200; el cliente lo evita."""
    transporte, pedidas = transporte_de_fixtures()
    async with httpx.AsyncClient(transport=transporte, base_url="https://api.bitget.com") as cli:
        rest = BitgetRest(venue="USDT-FUTURES", rate_limit=1000, client=cli)
        await rest.get_history_candles("BTCUSDT", end_time_ms=1, limit=1000)
    assert pedidas[-1].params["limit"] == "200"


async def test_get_candles_devuelve_modelos():
    transporte, pedidas = transporte_de_fixtures()
    async with httpx.AsyncClient(transport=transporte, base_url="https://api.bitget.com") as cli:
        rest = BitgetRest(venue="USDT-FUTURES", rate_limit=1000, client=cli)
        velas = await rest.get_candles("BTCUSDT")
    assert len(velas) == 200
    url = pedidas[-1]
    assert url.params["granularity"] == "1m"
    assert url.params["productType"] == "USDT-FUTURES"
    assert "endTime" not in url.params


async def test_error_de_negocio_de_bitget_lanza_excepcion():
    def handler(request):
        return httpx.Response(200, json={"code": "40020", "msg": "Parameter limit error", "data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://api.bitget.com") as cli:
        rest = BitgetRest(venue="USDT-FUTURES", rate_limit=1000, client=cli)
        try:
            await rest.get_tickers()
        except RuntimeError as exc:
            assert "40020" in str(exc)
        else:
            raise AssertionError("debería haber lanzado RuntimeError")
