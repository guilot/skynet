# tests/test_main.py
"""Regresión I-5: `__main__.py` no tenía ninguna cobertura, y ahí vivieron
los defectos de esta fase (los call sites de `orq.now_ms(ahora_ms())`, el
bucle de mantenimiento, `_marcar_ws_conectado`, `stale_after_ms` y el
cableado de `OutcomeTracker(horizons=...)`) -- la propia prueba de anclaje
del reloj (I6/C-1) reproducía el patrón de `__main__` en su propio cuerpo en
vez de ejercitar `__main__` directamente.

Los cuerpos de los bucles de `main()` se extrajeron a corrutinas con nombre
(`paso_tickers`/`paso_evaluador`/`paso_outcomes`/`paso_mantenimiento`/
`marcar_ws_conectado`), a nivel de módulo, con sus dependencias como
parámetros explícitos: se pueden ejercitar aquí con dobles de prueba, sin
levantar `httpx.AsyncClient`, `uvicorn` ni una `main()` entera. No toca red
ni reloj de pared real salvo `ahora_ms()`, cuyo único contrato es "milisegundos
desde epoch", que se comprueba sin comparar contra ningún reloj del exchange.
"""
from pathlib import Path

import pytest

from scanner_volumen.__main__ import (
    ahora_ms, marcar_ws_conectado, paso_evaluador, paso_mantenimiento,
    paso_outcomes, paso_tickers,
)
from scanner_volumen.app.orchestrator import Orchestrator
from scanner_volumen.app.state import ScannerState
from scanner_volumen.bitget.ws import WsEvent
from scanner_volumen.config import load_config
from scanner_volumen.engine.profile import build_profile
from scanner_volumen.models import Candle, Contract, Ticker
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import CandleRepo, ProfileRepo, SignalRepo
from scanner_volumen.universe.selector import UniverseSelector

MINUTO = 60_000
DIA = 1440 * MINUTO
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


def vela(ts, close=100.0, vol=100.0):
    return Candle(ts=ts, open=close, high=close, low=close, close=close,
                  base_vol=vol / close, quote_vol=vol)


class SupplyFalso:
    def market_cap(self, symbol):
        return 8e7

    async def refresh(self, symbols, now_ms):
        return 0


class BootstrapperFalso:
    def __init__(self, cfg):
        self._cfg = cfg

    async def bootstrap_symbol(self, symbol, now_ms):
        # vol=3000.0 (por defecto de `vela`, 100.0): por debajo de
        # min_profile_median_volume (config.toml) el filtro de libro fino
        # excluiría el símbolo justo después de que apply_universe lo
        # resolviera, y test_paso_tickers_refresca_el_universo_cuando_toca
        # comprueba que sigue en orq.profiles -- no ejercita esa puerta.
        velas = [vela(d * DIA + m * MINUTO, vol=3000.0) for d in range(14) for m in range(1440)]
        return build_profile(symbol, velas, self._cfg)

    def expect(self, symbols):
        pass

    def progress(self):
        return (1, 1)

    def mark_loaded(self, symbol):
        pass


@pytest.fixture
def orq(tmp_path):
    cfg = load_config(CONFIG_PATH)
    conn = open_db(tmp_path / "t.db")
    o = Orchestrator(
        cfg=cfg, rest=None, ws=None,
        candle_repo=CandleRepo(conn), profile_repo=ProfileRepo(conn),
        signal_repo=SignalRepo(conn), supply=SupplyFalso(),
        bootstrapper=BootstrapperFalso(cfg.profile),
    )
    yield o
    conn.close()


def test_ahora_ms_devuelve_milisegundos_no_decrecientes():
    # único punto donde main.py lee legítimamente el reloj de pared
    # (arranque en frío): solo se comprueba la conversión ns->ms, nunca se
    # compara contra un reloj del exchange -- eso lo cubre now_ms.
    a = ahora_ms()
    b = ahora_ms()
    assert a > 0
    assert b >= a


def test_marcar_ws_conectado_mueve_el_flag_del_estado():
    estado = ScannerState()
    assert estado.ws_connected is False
    marcar_ws_conectado(estado, True)
    assert estado.ws_connected is True
    marcar_ws_conectado(estado, False)
    assert estado.ws_connected is False


async def test_paso_evaluador_actualiza_now_ms_y_drena_reconectados(orq):
    """Cubre a la vez I-2(a) (now_ms del estado) y el drenado de
    `reconnected` -> `refill_gap` que antes solo vivía inline en `main()`."""
    base = 14 * DIA
    await orq.ensure_profile("AAAUSDT", now_ms=base)
    orq.set_ticker(Ticker("AAAUSDT", 100.0, 1.0, 5e6, 100.0, 0.0001, ts=base))
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(base)])
    )
    await orq.handle_ws_event(
        WsEvent(kind="snapshot", symbol="AAAUSDT", candles=[vela(base + MINUTO)])
    )
    assert "AAAUSDT" in orq.reconnected

    await paso_evaluador(orq, orq.bootstrapper, base + MINUTO)

    assert orq.reconnected == set()  # drenado
    assert orq.state.now_ms == base + MINUTO
    assert orq.state.snapshot("AAAUSDT") is not None
    assert (orq.state.bootstrap_done, orq.state.bootstrap_total) == (1, 1)


async def test_paso_outcomes_delega_en_el_tracker_con_el_now_ms_recibido():
    llamadas = []

    class TrackerFalso:
        def run_once(self, now_ms):
            llamadas.append(now_ms)
            return 0

    await paso_outcomes(TrackerFalso(), ahora=12_345)

    assert llamadas == [12_345]


async def test_paso_outcomes_no_propaga_un_fallo_del_tracker():
    class TrackerRoto:
        def run_once(self, now_ms):
            raise RuntimeError("Bitget no responde")

    await paso_outcomes(TrackerRoto(), ahora=0)  # no lanza


async def test_paso_mantenimiento_delega_en_run_maintenance(orq):
    base = 14 * DIA
    await orq.ensure_profile("AAAUSDT", now_ms=base)
    orq.candle_repo.save_many("AAAUSDT", [vela(base)])

    await paso_mantenimiento(orq, base + DIA)

    # run_maintenance real corrió de verdad: el perfil se recalculó
    assert "AAAUSDT" in orq.dirty


async def test_paso_mantenimiento_no_propaga_un_fallo(orq, monkeypatch):
    async def roto(now_ms):
        raise RuntimeError("disco lleno")

    monkeypatch.setattr(orq, "run_maintenance", roto)

    await paso_mantenimiento(orq, ahora=0)  # no lanza


def contrato(symbol):
    return Contract(symbol=symbol, base_coin=symbol[:-4], symbol_type="perpetual",
                     status="normal", is_rwa=False)


def ticker_universo(symbol, vol=5_000_000.0, ts=0):
    return Ticker(symbol, 100.0, 1.0, vol, 100.0, 0.0001, ts)


class RestFalso:
    def __init__(self, contratos, tickers):
        self._contratos = contratos
        self._tickers = tickers
        self.llamadas_contratos = 0
        self.llamadas_tickers = 0

    async def get_contracts(self):
        self.llamadas_contratos += 1
        return self._contratos

    async def get_tickers(self):
        self.llamadas_tickers += 1
        return self._tickers


async def test_paso_tickers_refresca_el_universo_cuando_toca(orq):
    rest = RestFalso([contrato("AAAUSDT")], [ticker_universo("AAAUSDT")])
    selector = UniverseSelector(orq.cfg.universe)
    refresh_ms = orq.cfg.universe.refresh_minutes * 60_000

    nuevo_ultimo = await paso_tickers(
        orq, rest, selector, orq.bootstrapper, orq.supply,
        ahora=refresh_ms, ultimo_universo=0,
        refresh_minutes=orq.cfg.universe.refresh_minutes,
    )

    assert nuevo_ultimo == refresh_ms  # el intervalo se cumplió: se refrescó
    assert rest.llamadas_contratos == 1
    assert rest.llamadas_tickers == 1
    assert orq.state.connected is True
    assert "AAAUSDT" in orq.profiles  # apply_universe se aplicó de verdad


async def test_paso_tickers_no_refresca_el_universo_antes_de_tiempo(orq):
    class RestExplota:
        async def get_contracts(self):
            raise AssertionError("no debería llamarse: el intervalo no llegó")

        async def get_tickers(self):
            raise AssertionError("no debería llamarse: el intervalo no llegó")

    selector = UniverseSelector(orq.cfg.universe)
    refresh_ms = orq.cfg.universe.refresh_minutes * 60_000

    nuevo_ultimo = await paso_tickers(
        orq, RestExplota(), selector, orq.bootstrapper, orq.supply,
        ahora=refresh_ms - 1, ultimo_universo=0,
        refresh_minutes=orq.cfg.universe.refresh_minutes,
    )

    assert nuevo_ultimo == 0
    assert orq.state.connected is True


async def test_paso_tickers_no_propaga_un_fallo_de_rest(orq):
    class RestRoto:
        async def get_contracts(self):
            raise RuntimeError("Bitget no responde")

        async def get_tickers(self):
            raise RuntimeError("Bitget no responde")

    selector = UniverseSelector(orq.cfg.universe)
    refresh_ms = orq.cfg.universe.refresh_minutes * 60_000

    nuevo_ultimo = await paso_tickers(
        orq, RestRoto(), selector, orq.bootstrapper, orq.supply,
        ahora=refresh_ms, ultimo_universo=0,
        refresh_minutes=orq.cfg.universe.refresh_minutes,
    )

    assert nuevo_ultimo == 0  # no avanzó: el refresco falló
    assert orq.state.connected is False
