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
    repo.set_equity_inicial("paper", 1000.0)
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


def test_con_proveedor_de_saldo_el_margen_sale_del_saldo_real(tmp_path):
    # Task 11, Step 1: con un proveedor que devuelve 850, el margen es
    # 17,00 (2% de 850) -NO el equity de la base (1000, fijado en el
    # fixture `cartera`), que es justo lo que se descarta al inyectar uno.
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn)
    repo.set_equity_inicial("real", 1000.0)
    cfg = BotConfig(enabled=True, modo="real", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    cartera = LivePortfolio(StrategyParams(), cfg, repo, proveedor_saldo=lambda: 850.0)
    assert cartera.equity() == pytest.approx(850.0)
    assert cartera.margen() == pytest.approx(17.0)
    conn.close()


def _cartera_real(tmp_path, proveedor_saldo):
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn)
    repo.set_equity_inicial("real", 1000.0)
    cfg = BotConfig(enabled=True, modo="real", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    return LivePortfolio(StrategyParams(), cfg, repo, proveedor_saldo=proveedor_saldo), conn


@pytest.mark.parametrize("saldo_invalido", [
    float("nan"), float("inf"), float("-inf"), -300.0, 0.0,
])
def test_un_saldo_invalido_del_proveedor_lanza(tmp_path, saldo_invalido):
    # Hallazgo de revision: -300 se propagaba tal cual hasta un margen
    # negativo y una orden real sin ninguna guarda. Un saldo que no sea
    # finito y positivo no puede dimensionar nada.
    cartera, conn = _cartera_real(tmp_path, lambda: saldo_invalido)
    with pytest.raises(ValueError):
        cartera.equity()
    conn.close()


def test_modo_real_sin_proveedor_registra_un_aviso(tmp_path, caplog):
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn)
    repo.set_equity_inicial("real", 1000.0)
    cfg = BotConfig(enabled=True, modo="real", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    cartera = LivePortfolio(StrategyParams(), cfg, repo)  # sin proveedor_saldo
    with caplog.at_level("ERROR"):
        cartera.equity()
    assert any("proveedor_saldo" in r.message for r in caplog.records)
    conn.close()


def test_modo_real_sin_proveedor_solo_avisa_una_vez(tmp_path, caplog):
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn)
    repo.set_equity_inicial("real", 1000.0)
    cfg = BotConfig(enabled=True, modo="real", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    cartera = LivePortfolio(StrategyParams(), cfg, repo)
    with caplog.at_level("ERROR"):
        cartera.equity()
        cartera.equity()
        cartera.equity()
    assert len(caplog.records) == 1
    conn.close()


def test_paper_sin_proveedor_no_avisa(cartera, caplog):
    with caplog.at_level("ERROR"):
        cartera.equity()
    assert caplog.records == []


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
    repo.set_equity_inicial("paper", 1000.0)
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
    repo.set_equity_inicial("paper", 1000.0)
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
    repo.set_equity_inicial("paper", 1000.0)
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


def test_el_descarte_se_persiste_ademas_de_contarse_en_ram(tmp_path):
    # A.3: el contador en RAM (`descartes`) se reinicia con el proceso; el
    # persistido (`bot_contadores`) es el que de verdad alimenta el informe
    # entre arranques.
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn)
    repo.set_equity_inicial("paper", 1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    cartera = LivePortfolio(StrategyParams(), cfg, repo)

    cartera.evaluar_entrada(tr(direction=Direction.NEUTRAL), abiertos=set(),
                            precio_mercado=100.0)
    cartera.evaluar_entrada(tr(direction=Direction.NEUTRAL), abiertos=set(),
                            precio_mercado=100.0)
    cartera.evaluar_entrada(tr(score=69.0), abiertos=set(), precio_mercado=100.0)

    assert cartera.descartes["NEUTRAL"] == 2
    assert repo.contadores("paper") == {"NEUTRAL": 2, "score bajo": 1}
    conn.close()


def test_una_entrada_valida_no_persiste_contador(tmp_path):
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn)
    repo.set_equity_inicial("paper", 1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    cartera = LivePortfolio(StrategyParams(), cfg, repo)

    assert cartera.evaluar_entrada(tr(), abiertos=set(), precio_mercado=100.0) is None
    assert repo.contadores("paper") == {}
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
