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
    MANTENIMIENTO_REINTENTO_MS, ahora_ms, marcar_ws_conectado, parse_args,
    paso_evaluador, paso_mantenimiento, paso_outcomes, paso_tickers,
)
from scanner_volumen.app.orchestrator import Orchestrator
from scanner_volumen.app.state import ScannerState
from scanner_volumen.bitget.ws import WsEvent
from scanner_volumen.config import load_config
from scanner_volumen.engine.profile import build_profile
from scanner_volumen.models import Candle, Contract, Ticker
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import (
    CandleRepo, MaintenanceRepo, ProfileRepo, SignalRepo,
)
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


@pytest.fixture
def maintenance_repo(tmp_path):
    """Conexión propia -no la de `orq`-: `paso_mantenimiento` recibe
    `maintenance_repo` como colaborador explícito, igual que `orq`, y los
    tests de este fichero solo necesitan que ambos existan, no que
    compartan el mismo fichero SQLite (en producción sí comparten `conn`,
    ver `main()`)."""
    conn = open_db(tmp_path / "maintenance.db")
    yield MaintenanceRepo(conn)
    conn.close()


def test_ahora_ms_devuelve_milisegundos_no_decrecientes():
    # único punto donde main.py lee legítimamente el reloj de pared
    # (arranque en frío): solo se comprueba la conversión ns->ms, nunca se
    # compara contra un reloj del exchange -- eso lo cubre now_ms.
    a = ahora_ms()
    b = ahora_ms()
    assert a > 0
    assert b >= a


def test_parse_args_usa_config_toml_por_defecto():
    """Sin `--config`, debe resolver a `config.toml` -el comportamiento
    hardcodeado anterior a este cambio- para que la unidad systemd de
    producción, que invoca `python -m scanner_volumen` sin argumentos, siga
    funcionando sin tocarla."""
    args = parse_args([])
    assert args.config == Path("config.toml")


def test_parse_args_admite_una_ruta_explicita():
    """`--config PATH` es lo que hace posible una segunda instancia (dev)
    apuntando a otra base de datos: dos procesos con `--config` distinto no
    pueden, por construcción, escribir en el mismo fichero."""
    args = parse_args(["--config", "config.dev.toml"])
    assert args.config == Path("config.dev.toml")


def test_config_inexistente_falla_con_filenotfounderror_claro(tmp_path):
    """`parse_args` no valida que la ruta exista -eso lo hace `load_config`
    al abrir el fichero-, pero el fallo debe ser un `FileNotFoundError` con
    la ruta en el mensaje, no una excepción oscura varias capas más abajo
    (p.ej. un KeyError de una sección ausente)."""
    ruta = tmp_path / "no_existe.toml"
    args = parse_args(["--config", str(ruta)])

    with pytest.raises(FileNotFoundError) as excinfo:
        load_config(args.config)
    assert excinfo.value.filename == str(ruta)


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


async def test_paso_mantenimiento_delega_en_run_maintenance(orq, maintenance_repo):
    base = 14 * DIA
    await orq.ensure_profile("AAAUSDT", now_ms=base)
    orq.candle_repo.save_many("AAAUSDT", [vela(base)])

    await paso_mantenimiento(
        orq, maintenance_repo, base + DIA, orq.cfg.maintenance.interval_hours
    )

    # run_maintenance real corrió de verdad: el perfil se recalculó
    assert "AAAUSDT" in orq.dirty


async def test_paso_mantenimiento_no_propaga_un_fallo(orq, maintenance_repo, monkeypatch):
    async def roto(now_ms):
        raise RuntimeError("disco lleno")

    monkeypatch.setattr(orq, "run_maintenance", roto)

    # no lanza
    await paso_mantenimiento(
        orq, maintenance_repo, ahora=0, interval_hours=orq.cfg.maintenance.interval_hours
    )
    assert maintenance_repo.get_last_completed_ms() is None  # el fallo no estampa nada


# --- persistencia del "último mantenimiento completado" a través de reinicios ---
#
# El bug de producción: `bucle_mantenimiento` dormía `interval_hours` ANTES
# de correr nada, así que bajo systemd (`Restart=always`) un proceso que se
# reinicia antes de acumular esas horas seguidas de vida nunca llegaba a
# correr mantenimiento -medido en real: perfiles de volumen (el propio
# denominador del RVOL) con seis días sin recalcularse, y la poda de
# `candles_1m` sin correr nunca. Estos tres tests cubren la decisión de
# `paso_mantenimiento`, que ahora se basa en `maintenance_repo` (sobrevive
# al reinicio) en vez de en cuánto lleva vivo el proceso actual.

async def test_paso_mantenimiento_vencido_al_arrancar_corre_de_inmediato(orq, maintenance_repo):
    """Un `maintenance_repo` vacío (nunca se completó mantenimiento, ni en
    este proceso ni en uno anterior) debe bastar para que la primera
    llamada -el "arranque"- lo corra ya, sin esperar `interval_hours`."""
    base = 14 * DIA
    await orq.ensure_profile("AAAUSDT", now_ms=base)
    # sin velas de verdad en candle_repo, `run_maintenance` no tendría nada
    # que recalcular de verdad (M3: candidato != resultado) y este test
    # dejaría de ejercitar la ruta "vencido, corre ya y estampa".
    orq.candle_repo.save_many("AAAUSDT", [vela(base)])
    assert maintenance_repo.get_last_completed_ms() is None

    espera_ms = await paso_mantenimiento(
        orq, maintenance_repo, base, orq.cfg.maintenance.interval_hours
    )

    assert maintenance_repo.get_last_completed_ms() == base  # corrió y quedó estampado
    assert espera_ms == int(orq.cfg.maintenance.interval_hours * 3600_000)


async def test_paso_mantenimiento_no_rerepite_poco_despues_de_completado(orq, maintenance_repo):
    """Un reinicio 10 minutos después de un mantenimiento exitoso no debe
    repetirlo: la decisión sobrevive al reinicio porque se lee de
    `maintenance_repo`, no del tiempo de vida del proceso actual."""
    base = 14 * DIA
    maintenance_repo.set_last_completed_ms(base)

    llamado = False

    async def espia(now_ms):
        nonlocal llamado
        llamado = True
        return True

    orq.run_maintenance = espia

    diez_min_despues = base + 10 * MINUTO
    espera_ms = await paso_mantenimiento(
        orq, maintenance_repo, diez_min_despues, orq.cfg.maintenance.interval_hours
    )

    assert llamado is False  # no se re-ejecutó
    assert maintenance_repo.get_last_completed_ms() == base  # sin cambios
    intervalo_ms = int(orq.cfg.maintenance.interval_hours * 3600_000)
    assert espera_ms == base + intervalo_ms - diez_min_despues


async def test_paso_mantenimiento_no_op_no_estampa_la_marca(orq, maintenance_repo):
    """La trampa del no-op: en un arranque en frío, `orq.profiles` está
    vacío (nada que recalcular todavía, el universo aún no se pobló), así
    que `run_maintenance` no hace ningún trabajo real. Estampar la marca
    igualmente empujaría el primer mantenimiento REAL `interval_hours` hacia
    el futuro -reintroduciendo, en una forma más difícil de detectar, el
    propio bug de programación que esta persistencia corrige."""
    assert orq.profiles == {}  # nada cargado todavía: arranque en frío puro

    espera_ms = await paso_mantenimiento(
        orq, maintenance_repo, 0, orq.cfg.maintenance.interval_hours
    )

    assert maintenance_repo.get_last_completed_ms() is None  # no se estampó
    assert espera_ms == MANTENIMIENTO_REINTENTO_MS  # reintenta pronto, no en 24h


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
