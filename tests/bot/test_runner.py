import pytest

from scanner_volumen.bot.broker import PaperBroker
from scanner_volumen.bot.portfolio import LivePortfolio
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.bot.runner import BotRunner
from scanner_volumen.config import BotConfig
from scanner_volumen.models import Direction, State
from scanner_volumen.storage.db import open_db
from scanner_volumen.strategy.model import ExitReason, StrategyParams, TransitionRow

MIN = 60_000


@pytest.fixture
def bot(tmp_path):
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn)
    repo.set_equity_inicial(1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    cartera = LivePortfolio(params, cfg, repo)
    runner = BotRunner(params, cfg, repo, PaperBroker(params), cartera)
    yield runner, repo
    conn.close()


def tr(symbol="A", ts=0, prev=State.NORMAL, new=State.HOT, price=100.0,
       score=75.0, direction=Direction.LONG):
    return TransitionRow(ts=ts, symbol=symbol, prev_state=prev, new_state=new,
                         price=price, direction=direction, score=score)


def precios(mapa):
    return lambda symbol: mapa.get(symbol)


async def test_una_entrada_abre_posicion_y_la_persiste(bot):
    runner, repo = bot
    await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)
    assert "A" in runner.abiertas
    filas = repo.abiertas("paper")
    assert len(filas) == 1
    assert filas[0]["symbol"] == "A"
    assert filas[0]["margin"] == pytest.approx(20.0)   # 2% de 1000
    assert filas[0]["notional"] == pytest.approx(400.0)  # 20 x 20


async def test_el_precio_de_entrada_es_el_de_mercado_no_el_de_la_senal(bot):
    runner, repo = bot
    # la senal decia 100 pero cuando el bot actua el mercado ya esta en 102
    await runner.on_tick([tr(price=100.0)], precios({"A": 102.0}), ahora=0)
    fila = repo.abiertas("paper")[0]
    assert fila["entry_price"] == pytest.approx(102.0)        # el ejecutado
    assert fila["entry_price_senal"] == pytest.approx(100.0)  # el de la senal
    # y el stop se ancla al ejecutado, no al de la senal
    assert runner.abiertas["A"].reglas.stop_price == pytest.approx(102.0 * 0.975)


async def test_un_descarte_no_abre_nada(bot):
    runner, repo = bot
    await runner.on_tick([tr(score=50.0)], precios({"A": 100.0}), ahora=0)
    assert runner.abiertas == {}
    assert repo.abiertas("paper") == []


async def test_sin_precio_no_se_entra(bot):
    runner, _ = bot
    await runner.on_tick([tr()], precios({}), ahora=0)
    assert runner.abiertas == {}


async def test_el_stop_cierra_la_posicion_y_registra_el_fill(bot):
    runner, repo = bot
    await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)
    # cae por debajo del stop (100 * 0.975 = 97.5)
    await runner.on_tick([], precios({"A": 97.0}), ahora=MIN)
    assert runner.abiertas == {}
    cerradas = repo.cerradas("paper")
    assert len(cerradas) == 1
    # size = notional/entry = (20 x 20)/100 = 4.0; caida de 3.0 -> pnl = -12.0
    assert cerradas[0]["pnl"] == pytest.approx(-12.0)
    fills = repo.fills_de(cerradas[0]["id"])
    assert [f["reason"] for f in fills] == [ExitReason.STOP.value]
    # con vela sintetica no hay mechas: se rellena al precio observado
    assert fills[0]["precio"] == pytest.approx(97.0)


async def test_una_transicion_a_signal_escala_dos_tramos(bot):
    runner, repo = bot
    await runner.on_tick([tr(new=State.WATCH)], precios({"A": 100.0}), ahora=0)
    await runner.on_tick([tr(ts=MIN, prev=State.WATCH, new=State.SIGNAL, price=120.0)],
                         precios({"A": 120.0}), ahora=MIN)
    pos = runner.abiertas["A"]
    assert pos.reglas.restante == pytest.approx(0.34)
    fills = repo.fills_de(pos.id)
    assert [f["reason"] for f in fills] == [
        ExitReason.SCALE_HOT.value, ExitReason.SCALE_SIGNAL.value]


async def test_el_tope_de_concurrencia_se_respeta(bot):
    runner, repo = bot
    simbolos = ["A", "B", "C", "D", "E", "F"]
    mapa = {s: 100.0 for s in simbolos}
    await runner.on_tick([tr(symbol=s) for s in simbolos], precios(mapa), ahora=0)
    assert len(runner.abiertas) == 5
    assert runner.portfolio.descartes["tope concurrencia"] == 1


async def test_el_equity_se_compone_tras_un_cierre(bot):
    runner, repo = bot
    await runner.on_tick([tr(new=State.WATCH)], precios({"A": 100.0}), ahora=0)
    # salta directo a EXTREME: cierra entero con ganancia
    await runner.on_tick(
        [tr(ts=MIN, prev=State.WATCH, new=State.EXTREME, price=130.0)],
        precios({"A": 130.0}), ahora=MIN)
    # extreme_run_min = 3 por defecto: aun corre. Se cierra al vencer.
    await runner.on_tick([], precios({"A": 130.0}), ahora=5 * MIN)
    assert runner.abiertas == {}
    # size = notional/entry = (20 x 20)/100 = 4.0; subida de 30 -> pnl = 120.0
    assert repo.equity("paper") == pytest.approx(1120.0)


async def test_pnl_short_con_precio_a_favor_es_exacto(bot):
    runner, repo = bot
    await runner.on_tick(
        [tr(new=State.WATCH, direction=Direction.SHORT)],
        precios({"A": 100.0}), ahora=0)
    # salta directo a EXTREME con el precio mas BAJO: para un short eso es
    # beneficio, no perdida
    await runner.on_tick(
        [tr(ts=MIN, prev=State.WATCH, new=State.EXTREME, price=70.0,
            direction=Direction.SHORT)],
        precios({"A": 70.0}), ahora=MIN)
    # extreme_run_min = 3 por defecto: se cierra al vencer
    await runner.on_tick([], precios({"A": 70.0}), ahora=5 * MIN)
    assert runner.abiertas == {}
    cerradas = repo.cerradas("paper")
    assert len(cerradas) == 1
    # size = notional/entry = (20 x 20)/100 = 4.0; caida de 30 a favor del
    # short -> pnl = 4.0 x 30 = 120.0 (positivo, pese a que el precio bajo)
    assert cerradas[0]["pnl"] == pytest.approx(120.0)


class _BrokerQueFallaAlCerrar:
    """Doble de prueba: abre normal pero revienta al cerrar, como simularia
    un timeout de red o cualquier otro fallo de la Fase 3 contra el exchange."""

    def __init__(self, params: StrategyParams) -> None:
        self._interno = PaperBroker(params)

    async def abrir(self, **kwargs):
        return await self._interno.abrir(**kwargs)

    async def cerrar(self, **kwargs):
        raise RuntimeError("fallo de red simulado")


async def test_fallo_del_broker_al_cerrar_degrada_la_posicion_sin_reventar(tmp_path):
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn)
    repo.set_equity_inicial(1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    cartera = LivePortfolio(params, cfg, repo)
    runner = BotRunner(params, cfg, repo, _BrokerQueFallaAlCerrar(params), cartera)

    await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)
    # cae por debajo del stop (97.5): la regla emite el STOP, pero el broker
    # falla al intentar cerrarlo -> la posicion debe quedar degradada, no rota
    await runner.on_tick([], precios({"A": 97.0}), ahora=MIN)

    assert "A" in runner.abiertas  # sigue ocupando su hueco de concurrencia
    pos = runner.abiertas["A"]
    assert pos.degradada is True
    assert repo.abiertas("paper")[0]["symbol"] == "A"  # sigue abierta=1 en la BD

    # un tick posterior no debe reventar: antes de este arreglo, PositionRules
    # lanzaba ValueError por la intencion sin confirmar en cada tick siguiente
    await runner.on_tick([], precios({"A": 90.0}), ahora=2 * MIN)
    assert runner.abiertas["A"].degradada is True

    conn.close()
