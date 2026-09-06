import pytest

from scanner_volumen.bot.report import construir_resumen, format_bloque_ejecucion
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.models import Direction
from scanner_volumen.storage.db import open_db
from scanner_volumen.strategy.model import ExitReason
from scanner_volumen.strategy.report import format_resumen

MIN = 60_000


@pytest.fixture
def repo(tmp_path):
    conn = open_db(tmp_path / "scanner.db")
    r = BotRepo(conn)
    r.set_equity_inicial(1000.0)
    yield r
    conn.close()


def _trade(repo, symbol="A", senal=100.0, ejecutado=100.0, salida_ref=110.0,
           salida=110.0, pnl=10.0):
    pid = repo.abrir(modo="paper", symbol=symbol, direction=Direction.LONG,
                     entry_ts=0, entry_price=ejecutado, entry_price_senal=senal,
                     margin=20.0, notional=400.0, size=4.0, fee_entrada=0.0)
    repo.registrar_fill(pid, ts=MIN, reason=ExitReason.EXTREME, fraction=1.0,
                        precio_referencia=salida_ref, precio=salida, comision=0.0)
    repo.cerrar(pid, close_ts=MIN, pnl=pnl, fees=0.0, max_rank=4)
    return pid


def test_el_resumen_usa_el_formato_del_backtest(repo):
    _trade(repo)
    salida = format_resumen(construir_resumen(repo, "paper", 1000.0))
    assert salida.startswith("== Bot en paper ==")
    assert "Trades ejecutados: 1" in salida
    assert "Equity: 1000.00 -> 1010.00" in salida
    assert "Runners (alcanzan SIGNAL+): 1 trades" in salida


def test_el_desvio_de_entrada_se_mide_en_bps_y_positivo_es_peor(repo):
    # LONG cuya senal era 100 y se ejecuto a 100.5: pago 50 bps de mas
    _trade(repo, senal=100.0, ejecutado=100.5)
    bloque = format_bloque_ejecucion(repo, "paper", descartes={}, cierres_tardios=0)
    assert "+50.0 bps" in bloque


def test_una_entrada_mejor_que_la_senal_da_desvio_negativo(repo):
    _trade(repo, senal=100.0, ejecutado=99.5)
    bloque = format_bloque_ejecucion(repo, "paper", descartes={}, cierres_tardios=0)
    assert "-50.0 bps" in bloque


def test_el_desvio_de_salida_se_desglosa_por_motivo(repo):
    # vendio a 109 cuando la regla pedia 110: 90.9 bps de coste
    _trade(repo, salida_ref=110.0, salida=109.0)
    bloque = format_bloque_ejecucion(repo, "paper", descartes={}, cierres_tardios=0)
    assert "EXTREME" in bloque
    assert "bps" in bloque


def test_el_bloque_publica_los_contadores(repo):
    _trade(repo)
    bloque = format_bloque_ejecucion(
        repo, "paper", descartes={"desvio": 3}, cierres_tardios=2)
    assert "Entradas descartadas por desvio: 3" in bloque
    assert "Cierres tardios por reinicio: 2" in bloque


def test_sin_trades_no_revienta(repo):
    bloque = format_bloque_ejecucion(repo, "paper", descartes={}, cierres_tardios=0)
    assert "sin datos" in bloque.lower() or "n=0" in bloque
