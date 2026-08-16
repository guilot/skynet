import httpx
import pytest

from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import SupplyRepo
from scanner_volumen.universe.supply import (
    SupplyCache, base_coin_of, match_coingecko,
)

HORA = 3_600_000

MERCADOS = [
    {"id": "bitcoin", "symbol": "btc", "market_cap": 1.2e12,
     "circulating_supply": 19.8e6, "fully_diluted_valuation": 1.3e12},
    {"id": "ethereum", "symbol": "eth", "market_cap": 2.3e11,
     "circulating_supply": 120e6, "fully_diluted_valuation": 2.3e11},
    {"id": "fake-btc", "symbol": "btc", "market_cap": 1000.0,
     "circulating_supply": 1.0, "fully_diluted_valuation": 1000.0},
]


@pytest.fixture
def repo(tmp_path):
    conn = open_db(tmp_path / "t.db")
    yield SupplyRepo(conn)
    conn.close()


def test_base_coin_of_quita_el_sufijo_usdt():
    assert base_coin_of("BTCUSDT") == "BTC"
    assert base_coin_of("1000PEPEUSDT") == "1000PEPE"


def test_match_elige_el_de_mayor_capitalizacion_ante_simbolos_duplicados():
    """CoinGecko tiene varios tokens con el mismo ticker; el real es el grande."""
    m = match_coingecko("BTC", MERCADOS)
    assert m["id"] == "bitcoin"


def test_match_sin_coincidencia_devuelve_none():
    assert match_coingecko("NOEXISTE", MERCADOS) is None


def cliente(respuesta=MERCADOS, status=200):
    def handler(request):
        return httpx.Response(status, json=respuesta)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="https://api.coingecko.com")


async def test_refresh_guarda_las_capitalizaciones(repo):
    async with cliente() as cli:
        cache = SupplyCache(repo, cli)
        actualizados = await cache.refresh(["BTCUSDT", "ETHUSDT"], now_ms=0)
    assert actualizados == 2
    assert cache.market_cap("BTCUSDT") == 1.2e12
    assert cache.market_cap("ETHUSDT") == 2.3e11


async def test_un_simbolo_sin_dato_devuelve_none_y_no_rompe(repo):
    async with cliente() as cli:
        cache = SupplyCache(repo, cli)
        await cache.refresh(["BTCUSDT", "RAROUSDT"], now_ms=0)
    assert cache.market_cap("RAROUSDT") is None


async def test_un_fallo_de_coingecko_no_lanza_excepcion(repo):
    """El scanner nunca debe quedar bloqueado por una API externa."""
    async with cliente(respuesta={"error": "rate limited"}, status=429) as cli:
        cache = SupplyCache(repo, cli)
        actualizados = await cache.refresh(["BTCUSDT"], now_ms=0)
    assert actualizados == 0
    assert cache.market_cap("BTCUSDT") is None


async def test_un_error_de_servidor_no_lanza_excepcion(repo):
    async with cliente(respuesta={"error": "caido"}, status=500) as cli:
        cache = SupplyCache(repo, cli)
        actualizados = await cache.refresh(["BTCUSDT"], now_ms=0)
    assert actualizados == 0
    assert cache.market_cap("BTCUSDT") is None


async def test_un_timeout_no_lanza_excepcion(repo):
    """Un timeout de red no es un HTTPStatusError: un except demasiado
    estrecho (solo status codes) dejaria escapar esta excepcion."""
    def handler(request):
        raise httpx.ConnectTimeout("tiempo agotado", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://api.coingecko.com") as cli:
        cache = SupplyCache(repo, cli)
        actualizados = await cache.refresh(["BTCUSDT"], now_ms=0)
    assert actualizados == 0
    assert cache.market_cap("BTCUSDT") is None


async def test_una_respuesta_malformada_no_lanza_excepcion(repo):
    """CoinGecko responde 200 pero con un cuerpo que no es la lista esperada."""
    async with cliente(respuesta={"no": "es una lista"}, status=200) as cli:
        cache = SupplyCache(repo, cli)
        actualizados = await cache.refresh(["BTCUSDT"], now_ms=0)
    assert actualizados == 0
    assert cache.market_cap("BTCUSDT") is None


async def test_los_datos_persistidos_sobreviven_a_un_reinicio(repo):
    async with cliente() as cli:
        await SupplyCache(repo, cli).refresh(["BTCUSDT"], now_ms=0)

    async with cliente(respuesta={"error": "caído"}, status=500) as cli:
        nueva = SupplyCache(repo, cli)   # carga desde SQLite al construirse
        await nueva.refresh(["BTCUSDT"], now_ms=HORA)
        assert nueva.market_cap("BTCUSDT") == 1.2e12


async def test_una_segunda_llamada_dentro_del_intervalo_no_repite_la_consulta(repo):
    """El guard de refresh_hours debe evitar golpear la API de nuevo, no solo
    devolver 0: si no fuera asi, una implementacion que jamas comprobara el
    intervalo pasaria igualmente los tests de arriba."""
    llamadas = {"n": 0}

    def handler(request):
        llamadas["n"] += 1
        return httpx.Response(200, json=MERCADOS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://api.coingecko.com") as cli:
        cache = SupplyCache(repo, cli, refresh_hours=1)
        primero = await cache.refresh(["BTCUSDT"], now_ms=0)
        llamadas_tras_primero = llamadas["n"]
        segundo = await cache.refresh(["BTCUSDT"], now_ms=HORA - 1)

    assert primero == 1
    assert llamadas_tras_primero > 0
    assert segundo == 0
    assert llamadas["n"] == llamadas_tras_primero  # no volvió a consultar la API


async def test_tras_agotarse_el_intervalo_vuelve_a_consultar(repo):
    llamadas = {"n": 0}

    def handler(request):
        llamadas["n"] += 1
        return httpx.Response(200, json=MERCADOS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://api.coingecko.com") as cli:
        cache = SupplyCache(repo, cli, refresh_hours=1)
        await cache.refresh(["BTCUSDT"], now_ms=0)
        llamadas_tras_primero = llamadas["n"]
        tercero = await cache.refresh(["BTCUSDT"], now_ms=HORA)

    assert tercero == 1
    assert llamadas["n"] > llamadas_tras_primero


async def test_un_fallo_no_bloquea_el_reintento_inmediato(repo):
    """Un fallo NO debe marcar la cache como recien refrescada: si lo hiciera,
    quedaria bloqueada 6 horas incluso tras un simple rate-limit pasajero."""
    llamadas = {"n": 0}

    def handler(request):
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json=MERCADOS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://api.coingecko.com") as cli:
        cache = SupplyCache(repo, cli, refresh_hours=6)
        primero = await cache.refresh(["BTCUSDT"], now_ms=0)
        assert primero == 0
        assert cache.market_cap("BTCUSDT") is None

        # muy poco despues, muy por debajo de refresh_hours: si el fallo
        # hubiera marcado la cache como refrescada, este segundo intento
        # se saltaria y devolveria 0 sin consultar la API de nuevo.
        segundo = await cache.refresh(["BTCUSDT"], now_ms=1_000)

    assert segundo == 1
    assert cache.market_cap("BTCUSDT") == 1.2e12
