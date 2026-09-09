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
import asyncio
from pathlib import Path

import pytest

from scanner_volumen.__main__ import (
    MANTENIMIENTO_REINTENTO_MS, ProveedorSaldo, ahora_ms,
    construir_piezas_del_bot, main, marcar_ws_conectado, parse_args,
    paso_evaluador, paso_mantenimiento, paso_outcomes, paso_saldo,
    paso_sondeo, paso_tickers, posiciones_del_bot,
)
from scanner_volumen.app.orchestrator import Orchestrator
from scanner_volumen.app.state import ScannerState
from scanner_volumen.bitget.private import (
    BitgetPrivate, ConfiguracionCuentaSymbol, SaldoCuenta,
)
from scanner_volumen.bitget.private import PosicionExchange as PosicionExchangeBitget
from scanner_volumen.bitget.ws import WsEvent
from scanner_volumen.bot.bitget_broker import BitgetBroker
from scanner_volumen.bot.broker import PaperBroker
from scanner_volumen.bot.frenos import MOTIVO_PARADA_EMERGENCIA, Frenos
from scanner_volumen.bot.model import OrdenEjecutada
from scanner_volumen.bot.modo import PAPER, REAL, REAL_LECTURA
from scanner_volumen.bot.portfolio import LivePortfolio
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.bot.runner import BotRunner
from scanner_volumen.config import BotConfig, load_config
from scanner_volumen.engine.profile import build_profile
from scanner_volumen.models import Candle, Contract, Direction, State, Ticker
from scanner_volumen.scoring.score import ScoreBreakdown
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import (
    CandleRepo, MaintenanceRepo, ProfileRepo, SignalRepo, StateTransitionRepo,
)
from scanner_volumen.strategy.model import StrategyParams, TransitionRow
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
        signal_repo=SignalRepo(conn), state_transition_repo=StateTransitionRepo(conn),
        supply=SupplyFalso(),
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


async def test_paso_evaluador_no_propaga_un_fallo_de_evaluate(orq, monkeypatch):
    # Con el registro de transiciones WATCH+ hay muchas mas escrituras; un
    # fallo transitorio de persistencia no debe tumbar el bucle del evaluador.
    def evaluate_roto(now_ms):
        raise RuntimeError("SQLite: disk I/O error")

    monkeypatch.setattr(orq, "evaluate", evaluate_roto)
    await paso_evaluador(orq, orq.bootstrapper, ahora=14 * DIA)  # no lanza


async def test_paso_evaluador_pasa_las_transiciones_al_bot(orq, monkeypatch):
    """Con `bot` distinto de `None`, `paso_evaluador` le pasa
    `orq.transiciones_evaluadas` -no el valor de retorno de `evaluate()`,
    que es una lista de `Transition` sin precio ni dirección, con la que el
    bot no podría operar- junto con la función de precio y el `ahora` del
    tick.

    Se fuerza una transición NORMAL -> WATCH real (mismo doble de
    `score_symbol` que usa `tests/app/test_orchestrator.py`) para poder
    comprobar el CONTENIDO de lo recibido, no solo que se recibió algo: con
    el fixture `orq` sin ese doble no habría ningún símbolo sucio y tanto
    `orq.transiciones_evaluadas` como el retorno de `evaluate()` serían
    listas vacías -en ese caso el test pasaría igual aunque `__main__.py`
    le entregara al bot el retorno crudo de `evaluate()`, que es justo el
    cableado incorrecto que esta prueba existe para cazar."""
    base = 14 * DIA
    await orq.ensure_profile("AAAUSDT", now_ms=base)
    orq.set_ticker(Ticker("AAAUSDT", 100.0, 1.0, 5e6, 100.0, 0.0001, ts=base))

    def score_falso(metrics, cfg):
        return ScoreBreakdown(total=55.0, raw_total=55.0, momentum=55.0,
                              demand=0.0, structure=0.0,
                              direction=Direction.LONG, components={})

    monkeypatch.setattr("scanner_volumen.app.orchestrator.score_symbol", score_falso)
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(base)])
    )

    class BotFalso:
        def __init__(self):
            self.recibido = None

        async def on_tick(self, transiciones, precio_de, ahora):
            # sin copiar: se comprueba más abajo que es el MISMO objeto que
            # `orq.transiciones_evaluadas`, no una lista distinta con el
            # mismo contenido.
            self.recibido = (transiciones, ahora)

    bot = BotFalso()
    await paso_evaluador(orq, orq.bootstrapper, ahora=base + 59_000, bot=bot)

    assert bot.recibido is not None
    assert bot.recibido[1] == base + 59_000
    recibidas = bot.recibido[0]
    assert recibidas is orq.transiciones_evaluadas  # exactamente esa lista
    assert len(recibidas) == 1  # la transición NORMAL -> WATCH forzada arriba
    assert recibidas[0].prev_state.name == "NORMAL"
    assert recibidas[0].new_state.name == "WATCH"
    assert recibidas[0].price is not None  # enriquecida: Transition no la lleva
    assert recibidas[0].direction is not None  # enriquecida: Transition no la lleva


async def test_un_fallo_del_bot_no_impide_actualizar_el_progreso_del_bootstrap(orq):
    """E: antes de este arreglo, `bot.on_tick` corría dentro del mismo
    `try` que envuelve a `bootstrapper.progress()` -una excepción del bot se
    tragaba la actualización del progreso de ESE tick. El bot debe tener su
    propio aislamiento, independiente del resto del paso."""
    class BotRoto:
        async def on_tick(self, transiciones, precio_de, ahora):
            raise RuntimeError("fallo simulado del bot")

    orq.state.bootstrap_done = orq.state.bootstrap_total = 0
    await paso_evaluador(orq, orq.bootstrapper, ahora=14 * DIA, bot=BotRoto())

    # BootstrapperFalso.progress() siempre devuelve (1, 1): si el fallo del
    # bot hubiera tumbado el resto del paso, esto seguiría en (0, 0).
    assert (orq.state.bootstrap_done, orq.state.bootstrap_total) == (1, 1)


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


# --- cableado del bot en los tres modos (Task 13) ---
#
# Estos tests protegen el punto donde una confusión pone dinero real en juego.
# El más importante es el del escalón intermedio (`real_lectura`): si alguien
# lo rompe, el bot empezaría a mandar órdenes reales creyendo que no.


class PrivadoFalso:
    """Doble de `BitgetPrivate` que no toca red. Cuenta las llamadas porque
    varios tests necesitan demostrar que el camino SÍ se recorrió, no solo
    que el resultado salió bien."""

    def __init__(self, saldos=(1000.0,), posiciones=()):
        self._saldos = list(saldos)
        self._posiciones = list(posiciones)
        self.consultas_de_saldo = 0
        self.consultas_de_posiciones = 0

    async def get_saldo(self):
        self.consultas_de_saldo += 1
        valor = self._saldos[min(self.consultas_de_saldo - 1, len(self._saldos) - 1)]
        if isinstance(valor, Exception):
            raise valor
        return SaldoCuenta(realizado=valor, disponible=valor, equity=valor,
                           pnl_no_realizado=0.0)

    async def get_posiciones(self):
        self.consultas_de_posiciones += 1
        return list(self._posiciones)

    async def get_configuracion_symbol(self, symbol):
        return ConfiguracionCuentaSymbol(
            margen_aislado=True,
            apalancamiento_long=float(StrategyParams().apalancamiento),
            apalancamiento_short=float(StrategyParams().apalancamiento),
        )


def _entorno_con_claves():
    return {
        "SCANNER_BITGET_KEY": "clave-de-prueba",
        "SCANNER_BITGET_SECRET": "secreto-de-prueba",
        "SCANNER_BITGET_PASSPHRASE": "passphrase-de-prueba",
    }


async def test_la_factoria_en_paper_no_construye_cliente_autenticado():
    """Y ni siquiera mira el entorno: se le pasan claves válidas y las
    ignora. El camino de paper no debe cargar credenciales que no necesita."""
    cfg = load_config(CONFIG_PATH)
    piezas = construir_piezas_del_bot(
        PAPER, StrategyParams(), cfg, http=None, entorno=_entorno_con_claves())

    assert piezas.privado is None
    assert isinstance(piezas.broker, PaperBroker)


async def test_la_factoria_en_real_lectura_lee_de_verdad_pero_ejecuta_en_paper():
    """EL TEST DEL ESCALÓN INTERMEDIO. `real_lectura` existe para conectarse
    a Bitget de verdad -saldo, posiciones, configuración de cuenta- sin poder
    mandar ni una orden. Si esta línea se rompe, el bot operaría con dinero
    real creyendo que está de prueba."""
    cfg = load_config(CONFIG_PATH)
    piezas = construir_piezas_del_bot(
        REAL_LECTURA, StrategyParams(), cfg, http=None,
        entorno=_entorno_con_claves())

    assert isinstance(piezas.privado, BitgetPrivate)  # sí lee
    assert isinstance(piezas.broker, PaperBroker)     # pero no ejecuta
    assert not isinstance(piezas.broker, BitgetBroker)


async def test_la_factoria_en_real_usa_el_broker_de_bitget():
    cfg = load_config(CONFIG_PATH)
    piezas = construir_piezas_del_bot(
        REAL, StrategyParams(), cfg, http=None, entorno=_entorno_con_claves())

    assert isinstance(piezas.privado, BitgetPrivate)
    assert isinstance(piezas.broker, BitgetBroker)


@pytest.mark.parametrize("variable", [
    "SCANNER_BITGET_KEY", "SCANNER_BITGET_SECRET", "SCANNER_BITGET_PASSPHRASE",
])
def test_la_factoria_en_real_sin_credenciales_falla_nombrando_la_variable(variable):
    """El mensaje nombra la VARIABLE que falta, nunca su contenido: una clave
    no puede aparecer en un log ni en un mensaje de excepción."""
    cfg = load_config(CONFIG_PATH)
    entorno = _entorno_con_claves()
    del entorno[variable]

    with pytest.raises(ValueError, match=variable):
        construir_piezas_del_bot(REAL, StrategyParams(), cfg, None, entorno)


def test_la_factoria_no_filtra_las_credenciales_en_el_mensaje_de_error():
    cfg = load_config(CONFIG_PATH)
    entorno = _entorno_con_claves()
    entorno["SCANNER_BITGET_SECRET"] = ""  # presente pero vacía

    with pytest.raises(ValueError) as excinfo:
        construir_piezas_del_bot(REAL, StrategyParams(), cfg, None, entorno)

    mensaje = str(excinfo.value)
    for valor in _entorno_con_claves().values():
        assert valor not in mensaje


async def test_la_factoria_rechaza_un_modo_desconocido():
    """La guarda redundante de `construir_piezas_del_bot`: un modo nuevo que
    nadie enseñó a esta función debe hacerla fallar, nunca caer por defecto
    en el broker que mueve dinero."""
    cfg = load_config(CONFIG_PATH)
    with pytest.raises(ValueError, match="modo efectivo desconocido"):
        construir_piezas_del_bot("casi_real", StrategyParams(), cfg, None,
                                 _entorno_con_claves())


def _config_real(tmp_path):
    """Copia de `config.toml` con `modo = "real"` y una base de datos propia.

    La base de datos se redirige a `tmp_path` para poder AFIRMAR que el
    proceso murió antes de abrirla: es lo que demuestra que la doble llave se
    comprueba antes de tocar nada, no a mitad del arranque."""
    texto = CONFIG_PATH.read_text(encoding="utf-8")
    assert 'modo = "paper"' in texto
    texto = texto.replace('modo = "paper"', 'modo = "real"')
    db = tmp_path / "no-deberia-existir.db"
    texto = texto.replace('db_path = "data/scanner.db"', f'db_path = "{db}"')
    destino = tmp_path / "config.toml"
    destino.write_text(texto, encoding="utf-8")
    return destino, db


async def test_main_no_arranca_con_modo_real_y_sin_la_variable(tmp_path, monkeypatch):
    """La doble llave de la Fase 3: `modo = "real"` en el fichero no basta.
    El `ValueError` de `resolver_modo` NO se captura -impedir el arranque es
    justamente su trabajo."""
    ruta, db = _config_real(tmp_path)
    monkeypatch.delenv("SCANNER_BOT_REAL", raising=False)

    with pytest.raises(ValueError, match="SCANNER_BOT_REAL"):
        await main(["--config", str(ruta)])

    # y murió ANTES de abrir la base de datos ni ninguna conexión de red
    assert not db.exists()


async def test_main_no_arranca_con_modo_real_y_una_variable_inventada(
    tmp_path, monkeypatch,
):
    ruta, db = _config_real(tmp_path)
    monkeypatch.setenv("SCANNER_BOT_REAL", "si")

    with pytest.raises(ValueError, match="SCANNER_BOT_REAL"):
        await main(["--config", str(ruta)])

    assert not db.exists()


# --- el proveedor de saldo (Step 2) ---


async def test_el_proveedor_lanza_mientras_no_haya_observado_un_saldo():
    """La decisión de diseño del Step 2: el proveedor NO PUEDE devolver un
    valor que no sea un saldo real observado. Sin esta propiedad, un `0.0`
    inicial se persistiría como referencia del freno de pérdida diaria y lo
    desactivaría el resto del día UTC, sobreviviendo a un reinicio."""
    proveedor = ProveedorSaldo(PrivadoFalso(), edad_maxima_s=60.0)

    with pytest.raises(ValueError, match="ningún saldo real"):
        proveedor()


async def test_el_proveedor_devuelve_el_ultimo_saldo_observado():
    privado = PrivadoFalso(saldos=(1234.5, 1200.0))
    proveedor = ProveedorSaldo(privado, edad_maxima_s=60.0)

    assert await proveedor.refrescar() == pytest.approx(1234.5)
    assert proveedor() == pytest.approx(1234.5)  # sin volver a preguntar
    assert privado.consultas_de_saldo == 1

    await proveedor.refrescar()
    assert proveedor() == pytest.approx(1200.0)


@pytest.mark.parametrize("espurio", [0.0, -5.0, float("nan"), float("inf")])
async def test_un_saldo_espurio_no_entra_en_el_cache(espurio):
    """Un valor inválido se rechaza en la ÚNICA puerta de entrada del caché,
    y no sustituye al último bueno: el freno sigue midiendo sobre una cifra
    real en vez de sobre la basura recién llegada."""
    privado = PrivadoFalso(saldos=(1000.0, espurio))
    proveedor = ProveedorSaldo(privado, edad_maxima_s=60.0)
    await proveedor.refrescar()

    with pytest.raises(ValueError):
        await proveedor.refrescar()

    assert proveedor() == pytest.approx(1000.0)


async def test_el_primer_saldo_espurio_deja_al_proveedor_sin_valor():
    """El caso que más importa: si la PRIMERA lectura del día es basura, el
    proveedor sigue sin tener nada que devolver -jamás un cero de fábrica."""
    proveedor = ProveedorSaldo(PrivadoFalso(saldos=(0.0,)), edad_maxima_s=60.0)

    with pytest.raises(ValueError):
        await proveedor.refrescar()
    with pytest.raises(ValueError, match="ningún saldo real"):
        proveedor()


async def test_un_saldo_demasiado_viejo_deja_de_darse_por_bueno():
    """Un saldo observado hace mucho es real pero ya no es una medida: si la
    red lleva caída lo bastante, el freno de pérdida diaria estaría
    comparando contra una foto congelada mientras la cuenta se hunde."""
    reloj = {"t": 0.0}
    proveedor = ProveedorSaldo(PrivadoFalso(), edad_maxima_s=60.0,
                               reloj=lambda: reloj["t"])
    await proveedor.refrescar()

    reloj["t"] = 60.0
    assert proveedor() == pytest.approx(1000.0)  # justo en el límite, vale

    reloj["t"] = 60.1
    with pytest.raises(ValueError, match="tolerados"):
        proveedor()

    # y se recupera en cuanto vuelve a haber una lectura fresca
    await proveedor.refrescar()
    assert proveedor() == pytest.approx(1000.0)


async def test_el_mismo_proveedor_alimenta_a_la_cartera_y_a_los_frenos(tmp_path):
    """El invariante de la Task 11: la referencia del freno y la medida del
    margen tienen que salir de la MISMA fuente. Se comprueba cambiando el
    saldo una sola vez y viendo que las dos puntas se mueven juntas."""
    conn = open_db(tmp_path / "bot.db")
    repo = BotRepo(conn)
    repo.set_equity_inicial("real", 1000.0)
    cfg_bot = BotConfig(enabled=True, modo="real", equity_inicial=1000.0,
                        desvio_max_entrada=0.0, perdida_diaria_max=0.05,
                        fichero_parada=str(tmp_path / "no-existe"))
    privado = PrivadoFalso(saldos=(1000.0, 900.0))
    proveedor = ProveedorSaldo(privado, edad_maxima_s=3600.0)
    await proveedor.refrescar()

    params = StrategyParams()
    cartera = LivePortfolio(params, cfg_bot, repo, proveedor)
    frenos = Frenos(cfg_bot, repo, "real", proveedor)

    assert cartera.equity() == pytest.approx(1000.0)
    frenos.registrar_saldo_del_dia(0, proveedor())
    assert frenos.puede_abrir(0) is None

    await proveedor.refrescar()  # la cuenta cae un 10%, con el tope en el 5%
    assert cartera.equity() == pytest.approx(900.0)
    assert frenos.puede_abrir(0) == "perdida diaria"
    conn.close()


async def test_paso_saldo_refresca_y_persiste_el_mismo_numero(tmp_path):
    """El contrato con la Task 13 escrito en `BotRepo.set_saldo_real`: lo que
    el informe enseña tiene que ser exactamente la cifra que dimensiona el
    margen, no una segunda lectura por otro camino."""
    conn = open_db(tmp_path / "bot.db")
    repo = BotRepo(conn)
    privado = PrivadoFalso(saldos=(1234.5,))
    proveedor = ProveedorSaldo(privado, edad_maxima_s=60.0)

    await paso_saldo(proveedor, repo, "real")

    assert repo.saldo_real("real") == pytest.approx(1234.5)
    assert proveedor() == pytest.approx(1234.5)
    conn.close()


async def test_paso_saldo_no_propaga_un_fallo_ni_persiste_nada(tmp_path):
    conn = open_db(tmp_path / "bot.db")
    repo = BotRepo(conn)
    privado = PrivadoFalso(saldos=(RuntimeError("Bitget no responde"),))
    proveedor = ProveedorSaldo(privado, edad_maxima_s=60.0)

    await paso_saldo(proveedor, repo, "real")  # no lanza

    assert repo.saldo_real("real") is None
    conn.close()


# --- traducción de posiciones del exchange ---


def test_las_posiciones_del_exchange_se_traducen_al_vocabulario_del_bot():
    traducidas = posiciones_del_bot([
        PosicionExchangeBitget(symbol="AAAUSDT", lado="long", tamano=4.0,
                               precio_entrada=100.0),
        PosicionExchangeBitget(symbol="BBBUSDT", lado="short", tamano=2.0,
                               precio_entrada=50.0),
    ])

    assert [p.symbol for p in traducidas] == ["AAAUSDT", "BBBUSDT"]
    assert [p.direction for p in traducidas] == [Direction.LONG, Direction.SHORT]
    assert traducidas[0].size == pytest.approx(4.0)
    assert traducidas[0].entry_price == pytest.approx(100.0)
    # el endpoint de posiciones no los trae: se dejan vacíos, no inventados
    assert traducidas[0].client_oid is None


def test_las_posiciones_de_tamano_no_positivo_se_descartan():
    """Una fila de tamaño 0 traducida sería una posición FANTASMA, con dos
    efectos contrarios y ambos malos: el sondeo creería que sigue abierta y
    no detectaría nunca su cierre, y la reconciliación de arranque la
    vetaría como ajena.

    El filtro es seguro se comporte como se comporte Bitget -si nunca
    devolviera filas así, no descarta nada-, pero sin este test nada impide
    quitarlo: se comprobó que eliminarlo dejaba la suite entera en verde."""
    traducidas = posiciones_del_bot([
        PosicionExchangeBitget(symbol="AAAUSDT", lado="long", tamano=0.0,
                               precio_entrada=100.0),
        PosicionExchangeBitget(symbol="BBBUSDT", lado="long", tamano=-1.0,
                               precio_entrada=100.0),
        PosicionExchangeBitget(symbol="CCCUSDT", lado="long", tamano=4.0,
                               precio_entrada=100.0),
    ])

    assert [p.symbol for p in traducidas] == ["CCCUSDT"]


def test_un_lado_desconocido_en_una_fila_de_tamano_cero_no_llega_a_lanzar():
    """El descarte va ANTES de interpretar el lado, a propósito: una fila
    vacía con el lado en blanco no debe tumbar la lectura entera del
    exchange -que es lo que haría el `ValueError` del lado desconocido."""
    assert posiciones_del_bot([
        PosicionExchangeBitget(symbol="AAAUSDT", lado="", tamano=0.0,
                               precio_entrada=0.0),
    ]) == []


def test_una_posicion_con_lado_desconocido_no_se_interpreta():
    """Interpretar mal el lado de una posición apalancada es peor que no
    interpretarlo: quien llama lo trata como "no se pudo leer el exchange"."""
    with pytest.raises(ValueError, match="lado desconocido"):
        posiciones_del_bot([
            PosicionExchangeBitget(symbol="AAAUSDT", lado="", tamano=4.0,
                                   precio_entrada=100.0),
        ])


# --- el solape sondeo/tick (Step 5): el fill duplicado ---
#
# Requisito de diseño trasladado desde la Task 9. El sondeo corre como tarea
# independiente del bucle evaluador: si pilla a `BotRunner._ejecutar`
# suspendido en un `await` del broker -después de mandar la orden de cierre y
# antes de registrarla- ve la posición desaparecida del exchange y la cierra
# en la base mientras el cierre normal sigue en vuelo. Resultado: DOS fills
# para un solo cierre.


class BrokerQueSeQueda(PaperBroker):
    """`PaperBroker` que se suspende dentro de `cerrar` hasta que el test lo
    suelta. Reproduce exactamente la ventana peligrosa: el bot ya decidió
    cerrar y está esperando al exchange."""

    def __init__(self, params):
        super().__init__(params)
        self.dentro_de_cerrar = asyncio.Event()
        self.puerta = asyncio.Event()

    async def cerrar(self, **kwargs):
        self.dentro_de_cerrar.set()
        await self.puerta.wait()
        return await super().cerrar(**kwargs)


async def _fill_de_cierre_falso(symbol):
    return OrdenEjecutada(ts=MINUTO, precio=97.0, cantidad=4.0, comision=0.0)


def _bot_con_broker_lento(tmp_path, symbol):
    """Un `BotRunner` en modo `real` con una posición ya abierta en `symbol` y
    un broker que se queda colgado en el siguiente cierre."""
    conn = open_db(tmp_path / "bot.db")
    repo = BotRepo(conn)
    repo.set_equity_inicial("real", 1000.0)
    cfg_bot = BotConfig(enabled=True, modo="real", equity_inicial=1000.0,
                        desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    broker = BrokerQueSeQueda(params)
    runner = BotRunner(params, cfg_bot, repo, broker,
                       LivePortfolio(params, cfg_bot, repo),
                       fill_de_cierre=_fill_de_cierre_falso)
    return conn, repo, runner, broker


def _transicion_de_entrada(symbol, precio=100.0):
    return TransitionRow(ts=0, symbol=symbol, prev_state=State.NORMAL,
                         new_state=State.HOT, price=precio,
                         direction=Direction.LONG, score=75.0)


async def test_sin_cerrojo_el_sondeo_duplica_el_cierre(tmp_path):
    """El peligro es REAL, no teórico: este test lo reproduce sin el cerrojo.
    Es lo que hace que el test siguiente signifique algo -sin este, "no se
    duplicó" podría deberse a que los dos caminos nunca se cruzaron."""
    symbol = "AAAUSDT"
    conn, repo, runner, broker = _bot_con_broker_lento(tmp_path, symbol)
    await runner.on_tick([_transicion_de_entrada(symbol)],
                         lambda s: 100.0, ahora=0)
    posicion_id = runner.abiertas[symbol].id

    # el tick cruza el stop (100 * 0.975) y se queda dentro del broker
    tarea_tick = asyncio.create_task(
        runner.on_tick([], lambda s: 90.0, ahora=MINUTO))
    await broker.dentro_de_cerrar.wait()

    # el sondeo entra justo en esa ventana: para el exchange ya no hay nada
    await runner.sondear_exchange([], ahora=MINUTO)

    broker.puerta.set()
    await tarea_tick

    assert len(repo.fills_de(posicion_id)) == 2  # el cierre se contó dos veces
    conn.close()


async def test_el_cerrojo_impide_que_el_sondeo_duplique_el_cierre(orq, tmp_path):
    """Mismo solape, con el cableado de verdad: `paso_evaluador` y
    `paso_sondeo` compartiendo el `asyncio.Lock` que arma `main()`. Los dos
    caminos corren -se comprueba que el sondeo llegó a preguntar al
    exchange-, pero nunca a la vez, y el cierre se registra UNA sola vez."""
    symbol = "AAAUSDT"
    conn, repo, runner, broker = _bot_con_broker_lento(tmp_path, symbol)
    await runner.on_tick([_transicion_de_entrada(symbol)],
                         lambda s: 100.0, ahora=0)
    posicion_id = runner.abiertas[symbol].id

    # el precio que verá `paso_evaluador` a través de `_precio_de(orq)`
    base = 14 * DIA
    orq.set_ticker(Ticker(symbol, 90.0, 1.0, 5e6, 100.0, 0.0001, ts=base))
    privado = PrivadoFalso(posiciones=())  # el exchange no reporta nada
    cerrojo = asyncio.Lock()

    tarea_tick = asyncio.create_task(
        paso_evaluador(orq, orq.bootstrapper, base, runner, cerrojo))
    await broker.dentro_de_cerrar.wait()  # el tick tiene el cerrojo tomado

    tarea_sondeo = asyncio.create_task(
        paso_sondeo(runner, privado, cerrojo, base))
    await asyncio.sleep(0)  # el sondeo se queda esperando el cerrojo

    broker.puerta.set()
    await asyncio.gather(tarea_tick, tarea_sondeo)

    assert privado.consultas_de_posiciones == 1  # el sondeo SÍ corrió
    assert len(repo.fills_de(posicion_id)) == 1  # y no duplicó el cierre
    assert repo.contadores("real").get("cierres detectados por sondeo") is None
    conn.close()


async def test_paso_sondeo_no_propaga_un_fallo_del_exchange(tmp_path):
    symbol = "AAAUSDT"
    conn, repo, runner, _ = _bot_con_broker_lento(tmp_path, symbol)

    class PrivadoRoto:
        async def get_posiciones(self):
            raise RuntimeError("Bitget no responde")

    await paso_sondeo(runner, PrivadoRoto(), asyncio.Lock(), MINUTO)  # no lanza
    conn.close()


async def test_paso_sondeo_pide_las_posiciones_con_el_cerrojo_tomado(tmp_path):
    """La foto del exchange y su interpretación tienen que ser atómicas
    respecto al tick: si se pidieran las posiciones ANTES de tomar el
    cerrojo, una posición abierta mientras se espera al tick se leería como
    "desaparecida del exchange" y se cerraría en la base recién abierta."""
    symbol = "AAAUSDT"
    conn, repo, runner, _ = _bot_con_broker_lento(tmp_path, symbol)
    cerrojo = asyncio.Lock()
    visto = {}

    class PrivadoQueMira:
        async def get_posiciones(self):
            visto["cerrojo_tomado"] = cerrojo.locked()
            return []

    await paso_sondeo(runner, PrivadoQueMira(), cerrojo, MINUTO)

    assert visto["cerrojo_tomado"] is True
    conn.close()


# --- la secuencia de arranque de los modos reales (ronda de revisión) ---
#
# HALLAZGO QUE MOTIVA ESTE BLOQUE: el revisor aplicó NUEVE mutaciones al
# cableado de `main()` -entre ellas `frenos=None` y `verificador=None`, que
# desconectan por completo dos criterios de aceptación de la fase- y la suite
# entera siguió verde en las nueve. El código estaba bien escrito y no lo
# defendía nada. Estos tests son esa red: arrancan `main()` de verdad con un
# cliente falso y se paran justo en la frontera de los bucles, para poder
# afirmar sobre lo que quedó cableado.


class _MainCapturada:
    """Lo que quedó construido cuando `main()` llegó a levantar los bucles."""

    def __init__(self):
        self.tareas: list[str] = []
        self.portfolio_kwargs = None
        self.frenos_args = None
        self.frenos = None
        self.runner_kwargs = None
        self.runner = None
        self.create_app_kwargs = None
        self.repo = None


def _config_para(tmp_path, modo, fichero_parada):
    """Copia de `config.toml` con el modo, la base de datos y el fichero de
    parada apuntando a `tmp_path`: nada de esto puede tocar producción."""
    texto = CONFIG_PATH.read_text(encoding="utf-8")
    texto = texto.replace('modo = "paper"', f'modo = "{modo}"')
    texto = texto.replace('db_path = "data/scanner.db"',
                          f'db_path = "{tmp_path / "scanner.db"}"')
    texto = texto.replace('fichero_parada = "data/parar_bot"',
                          f'fichero_parada = "{fichero_parada}"')
    destino = tmp_path / "config.toml"
    destino.write_text(texto, encoding="utf-8")
    return destino


async def _arrancar_main(monkeypatch, tmp_path, *, modo_config, valor_env,
                         privado=None, fichero_parada=None):
    """Corre `main()` hasta la frontera de los bucles y devuelve el cableado.

    `asyncio.gather` se sustituye por un doble que anota qué corrutinas iban a
    lanzarse y las cierra sin ejecutarlas: es exactamente el punto donde acaba
    el cableado y empieza el proceso vivo, y frenar ahí evita que ningún bucle
    llegue a tocar la red. El cliente privado es un doble, así que tampoco lo
    hacen ni el saldo ni las posiciones."""
    import scanner_volumen.__main__ as principal

    capt = _MainCapturada()
    fichero_parada = fichero_parada or (tmp_path / "parar_bot")
    ruta = _config_para(tmp_path, modo_config, fichero_parada)

    if valor_env is None:
        monkeypatch.delenv("SCANNER_BOT_REAL", raising=False)
    else:
        monkeypatch.setenv("SCANNER_BOT_REAL", valor_env)
    for nombre, valor in _entorno_con_claves().items():
        monkeypatch.setenv(nombre, valor)

    monkeypatch.setattr(principal, "get_code_revision", lambda cwd: "test")
    if privado is not None:
        monkeypatch.setattr(principal, "BitgetPrivate", lambda *a, **k: privado)

    real_portfolio, real_frenos = principal.LivePortfolio, principal.Frenos
    real_runner, real_repo = principal.BotRunner, principal.BotRepo
    real_create_app = principal.create_app

    def _portfolio(*a, **k):
        capt.portfolio_kwargs = (a, k)
        return real_portfolio(*a, **k)

    def _frenos(*a, **k):
        capt.frenos_args = (a, k)
        capt.frenos = real_frenos(*a, **k)
        return capt.frenos

    def _runner(*a, **k):
        capt.runner_kwargs = (a, k)
        capt.runner = real_runner(*a, **k)
        return capt.runner

    def _repo_falso(*a, **k):
        capt.repo = real_repo(*a, **k)
        return capt.repo

    def _create_app(*a, **k):
        capt.create_app_kwargs = k
        return real_create_app(*a, **k)

    monkeypatch.setattr(principal, "LivePortfolio", _portfolio)
    monkeypatch.setattr(principal, "Frenos", _frenos)
    monkeypatch.setattr(principal, "BotRunner", _runner)
    monkeypatch.setattr(principal, "BotRepo", _repo_falso)
    monkeypatch.setattr(principal, "create_app", _create_app)

    async def _gather_falso(*tareas, **kwargs):
        for tarea in tareas:
            capt.tareas.append(
                getattr(getattr(tarea, "cr_code", None), "co_name", repr(tarea)))
            tarea.close()  # ninguna llega a correr: aquí acaba el cableado
        return []

    monkeypatch.setattr(asyncio, "gather", _gather_falso)

    await main(["--config", str(ruta)])
    return capt


def _hoy_utc():
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


async def test_paper_arranca_las_mismas_tareas_de_siempre(monkeypatch, tmp_path):
    """La red que protege lo que corre HOY en producción: ni bucle de saldo,
    ni de sondeo, ni cliente autenticado, ni verificador de cuenta."""
    capt = await _arrancar_main(monkeypatch, tmp_path,
                                modo_config="paper", valor_env=None)

    assert capt.tareas == ["run", "bucle_tickers", "bucle_evaluador",
                           "bucle_outcomes", "bucle_mantenimiento", "serve"]
    assert capt.runner_kwargs[1]["verificador"] is None
    assert capt.runner_kwargs[1]["fill_de_cierre"] is None
    assert capt.portfolio_kwargs[0][3] is None  # sin proveedor de saldo
    assert capt.create_app_kwargs["modo"] == "paper"


async def test_en_paper_la_parada_de_emergencia_esta_cableada(monkeypatch, tmp_path):
    """I-3 de la revisión: `deploy/ENTORNO.md` documenta la parada de
    emergencia como un control vivo, y hasta esta ronda era inerte en el
    único modo que corre en producción. Solo actúa cuando un humano crea el
    fichero, así que encenderla no cambia nada mientras no exista."""
    parada = tmp_path / "parar_bot"
    capt = await _arrancar_main(monkeypatch, tmp_path, modo_config="paper",
                                valor_env=None, fichero_parada=parada)
    frenos = capt.runner_kwargs[1]["frenos"]
    assert frenos is not None

    assert frenos.puede_abrir(0) is None  # sin fichero, inerte
    parada.write_text("")
    assert frenos.puede_abrir(0) == MOTIVO_PARADA_EMERGENCIA
    parada.unlink()
    assert frenos.puede_abrir(0) is None  # y se reanuda sin reiniciar


async def test_en_paper_el_freno_de_perdida_diaria_no_se_evalua(monkeypatch, tmp_path):
    """La otra mitad de I-3: la pérdida diaria SÍ actúa sola, y en `paper`
    dejaría de abrir entradas que el backtest sí abre -rompiendo la
    comparación para la que existe ese modo-. Ni se consulta el saldo ni se
    persiste ninguna referencia del día."""
    capt = await _arrancar_main(monkeypatch, tmp_path,
                                modo_config="paper", valor_env=None)

    assert capt.frenos.puede_abrir(0) is None
    assert capt.repo.saldo_dia("paper", _hoy_utc()) is None


@pytest.mark.parametrize("valor_env,modo", [("lectura", REAL_LECTURA),
                                            ("ordenes", REAL)])
async def test_el_arranque_real_persiste_el_saldo_y_la_referencia_del_dia(
    monkeypatch, tmp_path, valor_env, modo,
):
    """Los dos pasos del arranque que ninguna mutación ponía en rojo:
    `set_saldo_real` (sin él, el informe dice "sin dato todavia" para
    siempre) y `registrar_saldo_del_dia` (la referencia del freno de pérdida
    diaria, que debe quedar fijada ANTES de la primera consulta)."""
    privado = PrivadoFalso(saldos=(1234.5,))
    capt = await _arrancar_main(monkeypatch, tmp_path, modo_config="real",
                                valor_env=valor_env, privado=privado)

    assert capt.repo.saldo_real(modo) == pytest.approx(1234.5)
    assert capt.repo.saldo_dia(modo, _hoy_utc()) == pytest.approx(1234.5)
    assert privado.consultas_de_saldo == 1


async def test_el_arranque_real_conecta_frenos_y_verificador(monkeypatch, tmp_path):
    """`frenos=None` y `verificador=None` desconectan dos criterios de
    aceptación de la fase (los frenos manuales y la verificación de cuenta) y
    la suite seguía verde con ambas mutaciones."""
    capt = await _arrancar_main(monkeypatch, tmp_path, modo_config="real",
                                valor_env="ordenes", privado=PrivadoFalso())

    assert capt.runner_kwargs[1]["frenos"] is not None
    assert capt.runner_kwargs[1]["verificador"] is not None
    assert capt.runner_kwargs[1]["fill_de_cierre"] is not None


async def test_main_comparte_una_sola_instancia_de_proveedor_de_saldo(
    monkeypatch, tmp_path,
):
    """I-2 de la revisión. El invariante de la Task 11 -la referencia del
    freno y la medida del margen salen de la MISMA fuente- estaba probado
    sobre un cableado construido a mano en el test, que demuestra que
    compartir uno funciona pero nunca que `main()` lo comparta. Esto último
    es lo que se comprueba aquí: la MISMA instancia, no una equivalente."""
    capt = await _arrancar_main(monkeypatch, tmp_path, modo_config="real",
                                valor_env="ordenes", privado=PrivadoFalso())

    proveedor_cartera = capt.portfolio_kwargs[0][3]
    proveedor_frenos = capt.frenos_args[0][3]
    assert proveedor_cartera is not None
    assert proveedor_frenos is proveedor_cartera


@pytest.mark.parametrize("valor_env,modo", [("lectura", REAL_LECTURA),
                                            ("ordenes", REAL)])
async def test_el_modo_efectivo_llega_al_bot_y_al_panel(
    monkeypatch, tmp_path, valor_env, modo,
):
    """`real` y `real_lectura` son dos libros contables distintos, y
    `cfg.bot.modo` solo sabe decir "real". Si el modo efectivo no llega, el
    panel avisaría de DINERO REAL en un modo que no puede mover un céntimo
    (o, peor, dejaría de avisar en el que sí)."""
    capt = await _arrancar_main(monkeypatch, tmp_path, modo_config="real",
                                valor_env=valor_env, privado=PrivadoFalso())

    assert capt.create_app_kwargs["modo"] == modo
    assert capt.runner_kwargs[0][1].modo == modo   # la BotConfig del runner
    assert capt.portfolio_kwargs[0][1].modo == modo
    assert capt.frenos_args[0][2] == modo


async def test_el_sondeo_y_la_reconciliacion_solo_ocurren_en_real(
    monkeypatch, tmp_path,
):
    """En `real_lectura` el broker es el de paper: las posiciones de ese libro
    son simuladas y no existen en Bitget. Ni se reconcilian contra el exchange
    ni se sondean -"no está en el exchange" es su estado normal."""
    privado_lectura = PrivadoFalso()
    capt = await _arrancar_main(monkeypatch, tmp_path, modo_config="real",
                                valor_env="lectura", privado=privado_lectura)
    assert "bucle_sondeo" not in capt.tareas
    assert "bucle_saldo" in capt.tareas          # el saldo sí se refresca
    assert privado_lectura.consultas_de_posiciones == 0

    otro = tmp_path / "real"
    otro.mkdir()
    privado_real = PrivadoFalso()
    capt = await _arrancar_main(monkeypatch, otro,
                                modo_config="real", valor_env="ordenes",
                                privado=privado_real)
    assert "bucle_sondeo" in capt.tareas
    assert "bucle_saldo" in capt.tareas
    assert privado_real.consultas_de_posiciones == 1  # reconcilió al arrancar


async def test_main_no_arranca_si_no_consigue_el_primer_saldo(monkeypatch, tmp_path):
    """La otra mitad del Step 2: que el proveedor lance sin saldo protege el
    caché, pero nada protegía la exigencia del arranque -envolver el
    `refrescar()` inicial en un `try/except: saldo = 0.0` dejaba la suite
    verde y reintroducía justo el cero contra el que se diseñó el paso."""
    privado = PrivadoFalso(saldos=(RuntimeError("Bitget no responde"),))

    with pytest.raises(RuntimeError, match="Bitget no responde"):
        await _arrancar_main(monkeypatch, tmp_path, modo_config="real",
                             valor_env="ordenes", privado=privado)
