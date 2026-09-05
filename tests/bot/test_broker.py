import pytest

from scanner_volumen.bot.broker import PaperBroker
from scanner_volumen.models import Direction
from scanner_volumen.strategy.model import StrategyParams

MIN = 60_000


async def test_abrir_rellena_al_precio_de_mercado():
    broker = PaperBroker(StrategyParams(comision_taker=0.0))
    orden = await broker.abrir(symbol="A", direction=Direction.LONG,
                               notional=400.0, precio_mercado=100.0, ts=MIN)
    assert orden.precio == pytest.approx(100.0)
    assert orden.cantidad == pytest.approx(4.0)   # 400 / 100
    assert orden.ts == MIN


async def test_abrir_cobra_comision_sobre_el_nocional():
    broker = PaperBroker(StrategyParams(comision_taker=0.0006))
    orden = await broker.abrir(symbol="A", direction=Direction.LONG,
                               notional=400.0, precio_mercado=100.0, ts=0)
    assert orden.comision == pytest.approx(0.24)  # 0.0006 * 400


async def test_cerrar_cobra_comision_sobre_lo_cerrado():
    broker = PaperBroker(StrategyParams(comision_taker=0.0006))
    orden = await broker.cerrar(symbol="A", direction=Direction.LONG,
                                cantidad=2.0, precio_mercado=110.0, ts=MIN)
    assert orden.precio == pytest.approx(110.0)
    assert orden.cantidad == pytest.approx(2.0)
    assert orden.comision == pytest.approx(0.132)  # 0.0006 * 2 * 110


async def test_el_paper_broker_no_toca_la_red():
    # garantia barata de que la Fase 2 no habla con Bitget: el broker de paper
    # no recibe ningun cliente HTTP ni lo construye
    broker = PaperBroker(StrategyParams())
    assert not hasattr(broker, "_client")
