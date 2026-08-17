import json
from pathlib import Path

import pytest

from scanner_volumen.bitget.parsing import (
    parse_candle, parse_candles, parse_contracts, parse_tickers,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def test_parsea_vela_rest_de_siete_campos():
    fila = ["1786825440000", "63052.3", "63052.9", "63052.2", "63052.3", "1.1006", "69395.3268"]
    c = parse_candle(fila)
    assert c.ts == 1786825440000
    assert c.open == 63052.3
    assert c.high == 63052.9
    assert c.close == 63052.3
    assert c.base_vol == 1.1006
    assert c.quote_vol == 69395.3268


def test_parsea_vela_ws_de_ocho_campos():
    """El WebSocket añade usdtVolume como octavo campo; se ignora."""
    fila = ["1786795500000", "62973.1", "62973.2", "62960", "62968.4",
            "12.3464", "777398.68719", "777398.68719"]
    c = parse_candle(fila)
    assert c.ts == 1786795500000
    assert c.quote_vol == 777398.68719


def test_vela_con_numero_de_campos_invalido_lanza_error():
    with pytest.raises(ValueError, match="7 u 8 campos"):
        parse_candle(["1", "2", "3"])


def test_parsea_contratos_del_fixture():
    contratos = parse_contracts(load("contracts_usdt_futures.json"))
    assert len(contratos) == 754
    btc = next(c for c in contratos if c.symbol == "BTCUSDT")
    assert btc.base_coin == "BTC"
    assert btc.is_rwa is False
    assert btc.status == "normal"
    assert btc.symbol_type == "perpetual"


def test_detecta_renta_variable_tokenizada():
    contratos = parse_contracts(load("contracts_usdt_futures.json"))
    rwa = [c for c in contratos if c.is_rwa]
    assert len(rwa) == 285
    assert any(c.symbol == "SNDKUSDT" for c in rwa)


def test_parsea_tickers_y_convierte_cambio_a_porcentaje():
    tickers = parse_tickers(load("tickers_usdt_futures.json"))
    assert len(tickers) == 754
    btc = next(t for t in tickers if t.symbol == "BTCUSDT")
    assert btc.last > 0
    # change24h en el JSON es fracción; el modelo lo guarda en porcentaje
    assert abs(btc.change_24h) < 100
    assert btc.volume_24h_usdt > 1_000_000
    assert btc.open_interest > 0


def test_parsea_lote_de_velas_rest():
    velas = parse_candles(load("candles_btc_1m.json"))
    assert len(velas) == 200
    # ordenadas ascendentemente por timestamp
    assert all(velas[i].ts < velas[i + 1].ts for i in range(len(velas) - 1))
    # separadas exactamente un minuto
    assert velas[1].ts - velas[0].ts == 60_000
