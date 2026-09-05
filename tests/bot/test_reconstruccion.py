import pytest

from scanner_volumen.bot.broker import PaperBroker
from scanner_volumen.bot.portfolio import LivePortfolio
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.bot.runner import BotRunner
from scanner_volumen.config import BotConfig
from scanner_volumen.models import Direction, State
from scanner_volumen.storage.db import open_db
from scanner_volumen.strategy.model import (
    CandleRow, ExitReason, StrategyParams, TransitionRow,
)

MIN = 60_000


def _nuevo_runner(conn):
    repo = BotRepo(conn)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    cartera = LivePortfolio(params, cfg, repo)
    return BotRunner(params, cfg, repo, PaperBroker(params), cartera), repo


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "scanner.db")
    BotRepo(c).set_equity_inicial(1000.0)
    yield c
    c.close()


def tr(symbol="A", ts=0, prev=State.NORMAL, new=State.WATCH, price=100.0,
       score=75.0):
    return TransitionRow(ts=ts, symbol=symbol, prev_state=prev, new_state=new,
                         price=price, direction=Direction.LONG, score=score)


def velas(inicio, precios):
    return [CandleRow(ts=inicio + i * MIN, open=p, high=p, low=p, close=p)
            for i, p in enumerate(precios)]


async def test_reconstruye_el_estado_del_motor_tras_un_reinicio(conn):
    # Bot 1: entra y escala a HOT (cobra el tramo y sube el stop a BE)
    bot1, repo = _nuevo_runner(conn)
    await bot1.on_tick([tr()], lambda s: 100.0, ahora=0)
    await bot1.on_tick([tr(ts=MIN, prev=State.WATCH, new=State.HOT, price=110.0)],
                       lambda s: 110.0, ahora=MIN)
    assert bot1.abiertas["A"].reglas.stop_en_be is True
    assert bot1.abiertas["A"].reglas.restante == pytest.approx(0.67)
    pnl_original = bot1.abiertas["A"].pnl_acumulado
    fees_originales = bot1.abiertas["A"].fees_acumuladas

    # Bot 2: proceso nuevo, mismo disco
    bot2, _ = _nuevo_runner(conn)
    historial = [tr(), tr(ts=MIN, prev=State.WATCH, new=State.HOT, price=110.0)]
    await bot2.reconstruir(
        transiciones_de=lambda s, desde: historial,
        velas_de=lambda s, desde: velas(0, [100.0, 110.0]),
        precio_de=lambda s: 110.0, ahora=2 * MIN,
    )
    pos = bot2.abiertas["A"]
    assert pos.reglas.stop_en_be is True                       # el BE se recupera
    assert pos.reglas.stop_price == pytest.approx(100.0)       # anclado a la entrada
    assert pos.reglas.restante == pytest.approx(0.67)          # el tramo ya cobrado
    assert pos.reglas.max_rank == State.HOT.rank
    assert bot2.cierres_tardios == 0
    # el PnL y las comisiones recompuestos deben coincidir EXACTAMENTE con los
    # de la posición original: es lo que valida que `_confirmar_registrado`
    # tomó el precio del fill guardado, no el de referencia de la intención.
    assert pos.pnl_acumulado == pytest.approx(pnl_original)
    assert pos.fees_acumuladas == pytest.approx(fees_originales)


async def test_una_salida_que_en_vivo_no_ocurrio_se_ejecuta_tarde(conn):
    bot1, repo = _nuevo_runner(conn)
    await bot1.on_tick([tr()], lambda s: 100.0, ahora=0)
    pid = bot1.abiertas["A"].id

    # el bot solo vio precios planos, pero la vela de 1m tiene una mecha que
    # perfora el stop (100 * 0.975 = 97.5): la replica sí la ve
    bot2, _ = _nuevo_runner(conn)
    con_mecha = [CandleRow(ts=0, open=100, high=100, low=100, close=100),
                 CandleRow(ts=MIN, open=100, high=100, low=97.0, close=100)]
    await bot2.reconstruir(
        transiciones_de=lambda s, desde: [tr()],
        velas_de=lambda s, desde: con_mecha,
        precio_de=lambda s: 99.0, ahora=2 * MIN,
    )
    assert bot2.abiertas == {}                 # la posicion se cerro
    assert bot2.cierres_tardios == 1
    fills = repo.fills_de(pid)
    assert fills[-1]["reason"] == ExitReason.STOP.value
    assert fills[-1]["tardio"] == 1
    assert fills[-1]["precio"] == pytest.approx(99.0)  # a mercado, ahora


async def test_sin_precio_actual_la_salida_divergente_deja_degradada(conn):
    # misma mecha que dispara el stop en la réplica, pero esta vez no hay
    # ningún precio actual con el que resolverla (símbolo sin ticker, feed
    # caído, etc.): no se puede ejecutar a mercado ni tampoco descartar.
    bot1, repo = _nuevo_runner(conn)
    await bot1.on_tick([tr()], lambda s: 100.0, ahora=0)

    bot2, _ = _nuevo_runner(conn)
    con_mecha = [CandleRow(ts=0, open=100, high=100, low=100, close=100),
                 CandleRow(ts=MIN, open=100, high=100, low=97.0, close=100)]
    await bot2.reconstruir(
        transiciones_de=lambda s, desde: [tr()],
        velas_de=lambda s, desde: con_mecha,
        precio_de=lambda s: None, ahora=2 * MIN,
    )
    # sigue ocupando su hueco de concurrencia -realmente sigue abierta en la
    # BD- pero marcada como degradada: el runner no le vuelve a tocar el
    # motor hasta el siguiente intento de reconstrucción.
    assert "A" in bot2.abiertas
    assert bot2.abiertas["A"].degradada is True
    assert bot2.cierres_tardios == 0


async def test_sin_transicion_de_entrada_no_revienta(conn):
    # caso degradado: la fila existe pero su transicion no esta en la BD
    bot1, repo = _nuevo_runner(conn)
    await bot1.on_tick([tr()], lambda s: 100.0, ahora=0)

    bot2, _ = _nuevo_runner(conn)
    await bot2.reconstruir(
        transiciones_de=lambda s, desde: [],
        velas_de=lambda s, desde: [],
        precio_de=lambda s: 100.0, ahora=2 * MIN,
    )
    # no la adopta, pero tampoco tumba el arranque
    assert bot2.abiertas == {}
