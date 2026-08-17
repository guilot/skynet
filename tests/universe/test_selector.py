import json
from pathlib import Path

from scanner_volumen.bitget.parsing import parse_contracts, parse_tickers
from scanner_volumen.config import UniverseConfig
from scanner_volumen.models import Contract, Ticker
from scanner_volumen.universe.selector import UniverseSelector

FIXTURES = Path(__file__).parent.parent / "fixtures"
MINUTO = 60_000


def cfg(**kwargs):
    base = dict(
        min_volume_24h=1_000_000,
        min_profile_median_volume=2_000.0,  # UniverseSelector no lo usa; solo lo exige el dataclass
        max_symbols=150,
        exclude_rwa=True,
        refresh_minutes=15,
        exit_grace_minutes=60,
    )
    base.update(kwargs)
    return UniverseConfig(**base)


def contrato(symbol, is_rwa=False, status="normal"):
    return Contract(symbol=symbol, base_coin=symbol[:-4], symbol_type="perpetual",
                    status=status, is_rwa=is_rwa)


def ticker(symbol, volumen):
    return Ticker(symbol=symbol, last=1.0, change_24h=0.0, volume_24h_usdt=volumen,
                  open_interest=0.0, funding_rate=0.0, ts=0)


def test_excluye_renta_variable_tokenizada():
    sel = UniverseSelector(cfg())
    contratos = [contrato("BTCUSDT"), contrato("SNDKUSDT", is_rwa=True)]
    tickers = [ticker("BTCUSDT", 5e6), ticker("SNDKUSDT", 5e6)]
    r = sel.select(contratos, tickers, now_ms=0)
    assert r.symbols == {"BTCUSDT"}


def test_excluye_por_debajo_del_volumen_minimo():
    sel = UniverseSelector(cfg(min_volume_24h=1_000_000))
    contratos = [contrato("AAAUSDT"), contrato("BBBUSDT")]
    tickers = [ticker("AAAUSDT", 2e6), ticker("BBBUSDT", 500_000)]
    r = sel.select(contratos, tickers, now_ms=0)
    assert r.symbols == {"AAAUSDT"}


def test_excluye_contratos_que_no_estan_normales():
    sel = UniverseSelector(cfg())
    contratos = [contrato("AAAUSDT"), contrato("BBBUSDT", status="halt")]
    tickers = [ticker("AAAUSDT", 5e6), ticker("BBBUSDT", 5e6)]
    r = sel.select(contratos, tickers, now_ms=0)
    assert r.symbols == {"AAAUSDT"}


def test_recorta_a_max_symbols_por_volumen_descendente():
    sel = UniverseSelector(cfg(max_symbols=2))
    contratos = [contrato(f"S{i}USDT") for i in range(5)]
    tickers = [ticker(f"S{i}USDT", 10e6 - i * 1e6) for i in range(5)]
    r = sel.select(contratos, tickers, now_ms=0)
    assert r.symbols == {"S0USDT", "S1USDT"}


def test_symbols_va_ordenado_por_volumen_para_priorizar_el_bootstrap():
    sel = UniverseSelector(cfg())
    contratos = [contrato("AAAUSDT"), contrato("BBBUSDT"), contrato("CCCUSDT")]
    tickers = [ticker("AAAUSDT", 2e6), ticker("BBBUSDT", 9e6), ticker("CCCUSDT", 5e6)]
    r = sel.select(contratos, tickers, now_ms=0)
    assert r.ordered == ["BBBUSDT", "CCCUSDT", "AAAUSDT"]


def test_un_simbolo_que_sale_no_se_elimina_hasta_agotar_la_gracia():
    sel = UniverseSelector(cfg(exit_grace_minutes=60))
    contratos = [contrato("AAAUSDT")]
    sel.select(contratos, [ticker("AAAUSDT", 5e6)], now_ms=0)

    # cae por debajo del umbral: sigue dentro, en gracia
    r = sel.select(contratos, [ticker("AAAUSDT", 100)], now_ms=30 * MINUTO)
    assert r.symbols == {"AAAUSDT"}
    assert r.removed == frozenset()

    # pasada la gracia, sale de verdad
    r = sel.select(contratos, [ticker("AAAUSDT", 100)], now_ms=61 * MINUTO)
    assert r.symbols == frozenset()
    assert r.removed == {"AAAUSDT"}


def test_volver_a_superar_el_umbral_cancela_la_gracia():
    sel = UniverseSelector(cfg(exit_grace_minutes=60))
    contratos = [contrato("AAAUSDT")]
    sel.select(contratos, [ticker("AAAUSDT", 5e6)], now_ms=0)
    sel.select(contratos, [ticker("AAAUSDT", 100)], now_ms=30 * MINUTO)
    sel.select(contratos, [ticker("AAAUSDT", 5e6)], now_ms=40 * MINUTO)
    r = sel.select(contratos, [ticker("AAAUSDT", 5e6)], now_ms=200 * MINUTO)
    assert r.symbols == {"AAAUSDT"}
    assert r.removed == frozenset()


def test_added_solo_contiene_simbolos_realmente_nuevos():
    sel = UniverseSelector(cfg())
    contratos = [contrato("AAAUSDT"), contrato("BBBUSDT")]
    r1 = sel.select(contratos, [ticker("AAAUSDT", 5e6)], now_ms=0)
    assert r1.added == {"AAAUSDT"}
    r2 = sel.select(contratos, [ticker("AAAUSDT", 5e6), ticker("BBBUSDT", 5e6)], now_ms=MINUTO)
    assert r2.added == {"BBBUSDT"}


def test_sobre_datos_reales_produce_un_universo_plausible():
    sel = UniverseSelector(cfg())
    contratos = parse_contracts(json.loads((FIXTURES / "contracts_usdt_futures.json").read_text()))
    tickers = parse_tickers(json.loads((FIXTURES / "tickers_usdt_futures.json").read_text()))
    r = sel.select(contratos, tickers, now_ms=0)
    assert 50 <= len(r.symbols) <= 150
    assert "SNDKUSDT" not in r.symbols  # es RWA
    assert "BTCUSDT" in r.symbols
