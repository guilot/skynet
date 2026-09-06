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
           salida=110.0, pnl=10.0, direction=Direction.LONG):
    pid = repo.abrir(modo="paper", symbol=symbol, direction=direction,
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
    # LONG: vendio a 109 cuando la regla pedia 110.
    # bps = (110 - 109) / 110 * 10000 = 90.909... -> "+90.9 bps"
    _trade(repo, salida_ref=110.0, salida=109.0)
    bloque = format_bloque_ejecucion(repo, "paper", descartes={}, cierres_tardios=0)
    assert "EXTREME" in bloque
    assert "+90.9 bps" in bloque


def test_el_desvio_de_entrada_en_short_sufre_si_se_vende_por_debajo(repo):
    # SHORT: entrar es vender. La senal pedia 100 y se vendio a 99 (por
    # debajo): peor para nosotros porque un short quiere abrir a precio alto.
    # bps = (100 - 99) / 100 * 10000 = 100.0 -> "+100.0 bps".
    # Si el signo estuviera invertido (se aplicara la formula de LONG), daria
    # -100.0 bps: un numero claramente distinto, no solo un redondeo.
    _trade(repo, senal=100.0, ejecutado=99.0, direction=Direction.SHORT)
    bloque = format_bloque_ejecucion(repo, "paper", descartes={}, cierres_tardios=0)
    assert "+100.0 bps" in bloque


def test_el_desvio_de_salida_en_short_sufre_si_se_recompra_por_encima(repo):
    # SHORT: salir es recomprar. La regla pedia 100 y se recompro a 101 (por
    # encima): peor para nosotros porque pagamos mas de lo que la regla
    # queria para cerrar el short.
    # bps = (101 - 100) / 100 * 10000 = 100.0 -> "+100.0 bps".
    # Con la formula de LONG (invertida) daria -100.0 bps.
    _trade(repo, salida_ref=100.0, salida=101.0, direction=Direction.SHORT)
    bloque = format_bloque_ejecucion(repo, "paper", descartes={}, cierres_tardios=0)
    assert "EXTREME" in bloque
    assert "+100.0 bps" in bloque


def test_el_bloque_publica_los_contadores(repo):
    _trade(repo)
    bloque = format_bloque_ejecucion(
        repo, "paper", descartes={"desvio": 3}, cierres_tardios=2)
    assert "Entradas descartadas por desvio: 3" in bloque
    assert "Cierres tardios por reinicio: 2" in bloque


def test_sin_trades_no_revienta(repo):
    bloque = format_bloque_ejecucion(repo, "paper", descartes={}, cierres_tardios=0)
    assert "sin datos" in bloque.lower() or "n=0" in bloque


def test_un_fill_tardio_en_una_posicion_abierta_tambien_cuenta(repo):
    # Una salida parcial tardia no cierra el trade: la posicion sigue abierta
    # (nunca se llama a repo.cerrar). El contador de cierres tardios debe
    # verla igual, no solo las de posiciones ya cerradas.
    pid = repo.abrir(modo="paper", symbol="A", direction=Direction.LONG,
                     entry_ts=0, entry_price=100.0, entry_price_senal=100.0,
                     margin=20.0, notional=400.0, size=4.0, fee_entrada=0.0)
    repo.registrar_fill(pid, ts=MIN, reason=ExitReason.SCALE_HOT, fraction=0.33,
                        precio_referencia=100.0, precio=100.0, comision=0.0,
                        tardio=True)
    bloque = format_bloque_ejecucion(repo, "paper", descartes={}, cierres_tardios=0)
    assert "Cierres tardios por reinicio: 1" in bloque
