import pytest

from scanner_volumen.bot.portfolio import LivePortfolio
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.config import BotConfig
from scanner_volumen.models import Direction, State
from scanner_volumen.storage.db import open_db
from scanner_volumen.strategy.model import StrategyParams, TransitionRow

HORA = 3_600_000


@pytest.fixture
def cartera(tmp_path):
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn)
    repo.set_equity_inicial(1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    yield LivePortfolio(StrategyParams(), cfg, repo)
    conn.close()


def tr(symbol="A", ts=0, score=75.0, direction=Direction.LONG, price=100.0):
    return TransitionRow(ts=ts, symbol=symbol, prev_state=State.NORMAL,
                         new_state=State.HOT, price=price, direction=direction,
                         score=score)


def test_margen_es_el_2_por_ciento_del_equity(cartera):
    assert cartera.equity() == pytest.approx(1000.0)
    assert cartera.margen() == pytest.approx(20.0)


def test_una_entrada_valida_no_se_descarta(cartera):
    assert cartera.evaluar_entrada(tr(), abiertos=set(), precio_mercado=100.0) is None


def test_descarta_neutral(cartera):
    motivo = cartera.evaluar_entrada(tr(direction=Direction.NEUTRAL),
                                     abiertos=set(), precio_mercado=100.0)
    assert motivo == "NEUTRAL"
    assert cartera.descartes["NEUTRAL"] == 1


def test_descarta_score_bajo(cartera):
    assert cartera.evaluar_entrada(tr(score=69.0), abiertos=set(),
                                   precio_mercado=100.0) == "score bajo"


def test_descarta_simbolo_ya_abierto(cartera):
    assert cartera.evaluar_entrada(tr(symbol="A"), abiertos={"A"},
                                   precio_mercado=100.0) == "simbolo abierto"


def test_descarta_por_tope_de_concurrencia(cartera):
    abiertos = {"B", "C", "D", "E", "F"}  # max_concurrentes por defecto = 5
    assert cartera.evaluar_entrada(tr(symbol="A"), abiertos=abiertos,
                                   precio_mercado=100.0) == "tope concurrencia"


def test_descarta_par_congelado(cartera):
    for i in range(3):  # 3 perdidas seguidas en menos de 1h congelan 3h
        cartera.registrar_cierre("A", close_ts=i * 60_000, pnl=-1.0)
    assert cartera.evaluar_entrada(tr(symbol="A", ts=HORA), abiertos=set(),
                                   precio_mercado=100.0) == "par congelado"


def test_el_desvio_desactivado_nunca_descarta(cartera):
    # desvio_max_entrada = 0.0 => el filtro esta apagado, entre lo que entre
    assert cartera.evaluar_entrada(tr(price=100.0), abiertos=set(),
                                   precio_mercado=200.0) is None


def test_el_desvio_activo_descarta_movimientos_adversos(tmp_path):
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn)
    repo.set_equity_inicial(1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.005)  # 0.5%
    cartera = LivePortfolio(StrategyParams(), cfg, repo)
    # LONG cuya senal era 100 y el mercado ya esta en 101 (+1%): perseguir
    assert cartera.evaluar_entrada(tr(price=100.0), abiertos=set(),
                                   precio_mercado=101.0) == "desvio"
    conn.close()


def test_el_desvio_activo_permite_movimientos_favorables(tmp_path):
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn)
    repo.set_equity_inicial(1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.005)
    cartera = LivePortfolio(StrategyParams(), cfg, repo)
    # LONG cuya senal era 100 y el mercado bajo a 99: entrada MEJOR, se acepta
    assert cartera.evaluar_entrada(tr(price=100.0), abiertos=set(),
                                   precio_mercado=99.0) is None
    conn.close()


def test_el_equity_baja_con_las_perdidas_y_el_margen_con_el(cartera, tmp_path):
    # el capital es un saldo vivo: si el bot pierde, arriesga menos
    pid = cartera._repo.abrir(
        modo="paper", symbol="A", direction=Direction.LONG, entry_ts=0,
        entry_price=100.0, entry_price_senal=100.0, margin=20.0,
        notional=400.0, size=4.0, fee_entrada=0.0)
    cartera._repo.cerrar(pid, close_ts=60_000, pnl=-150.0, fees=1.0, max_rank=1)
    assert cartera.equity() == pytest.approx(850.0)
    assert cartera.margen() == pytest.approx(17.0)


def test_el_desvio_activo_descarta_movimientos_adversos_en_short(tmp_path):
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn)
    repo.set_equity_inicial(1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.005)  # 0.5%
    cartera = LivePortfolio(StrategyParams(), cfg, repo)
    # SHORT cuya senal era 100 y el mercado ya bajo a 99 (-1%): perseguir, se rechaza
    assert cartera.evaluar_entrada(tr(price=100.0, direction=Direction.SHORT),
                                   abiertos=set(), precio_mercado=99.0) == "desvio"
    # SHORT cuya senal era 100 y el mercado subio a 101: entrada MEJOR, se acepta
    assert cartera.evaluar_entrada(tr(price=100.0, direction=Direction.SHORT),
                                   abiertos=set(), precio_mercado=101.0) is None
    conn.close()


def test_el_orden_de_los_descartes_respeta_la_prioridad(cartera):
    # NEUTRAL gana sobre score bajo
    motivo = cartera.evaluar_entrada(
        tr(direction=Direction.NEUTRAL, score=69.0),
        abiertos=set(), precio_mercado=100.0)
    assert motivo == "NEUTRAL"

    # simbolo abierto gana sobre tope concurrencia
    # El conjunto abiertos tiene 4 pares (B, C, D, E) y A esta en el abierto
    abiertos = {"A", "B", "C", "D", "E"}  # 5 pares, por lo que tope tambien se cumple
    motivo = cartera.evaluar_entrada(
        tr(symbol="A"), abiertos=abiertos, precio_mercado=100.0)
    assert motivo == "simbolo abierto"
