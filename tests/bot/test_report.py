import pytest

from scanner_volumen.bot.report import construir_resumen, format_bloque_ejecucion
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.models import Direction
from scanner_volumen.storage.db import open_db
from scanner_volumen.strategy.model import ExitReason
from scanner_volumen.strategy.report import format_resumen

MIN = 60_000
_SIN_PASAR = object()  # sentinel: distingue "no se pasó precio_regla" de "se pasó None"


@pytest.fixture
def repo(tmp_path):
    conn = open_db(tmp_path / "scanner.db")
    r = BotRepo(conn)
    r.set_equity_inicial("paper", 1000.0)
    yield r
    conn.close()


def _trade(repo, symbol="A", senal=100.0, ejecutado=100.0, salida_ref=110.0,
           salida=110.0, pnl=10.0, direction=Direction.LONG,
           reason=ExitReason.STOP, precio_regla=_SIN_PASAR):
    """`reason=STOP` por defecto porque SÍ tiene un nivel prometido contra el
    que medir (a diferencia de EXTREME, que cierra a mercado adrede y por
    tanto no se promedia -ver `test_una_salida_sin_precio_regla_no_se_
    promedia`). `precio_regla`, si no se pasa, es `salida_ref`: en estos
    tests "lo que la regla pedía" y "la referencia de la intención" son el
    mismo número, salvo que el test pida explícitamente `None`."""
    if precio_regla is _SIN_PASAR:
        precio_regla = salida_ref
    pid = repo.abrir(modo="paper", symbol=symbol, direction=direction,
                     entry_ts=0, entry_price=ejecutado, entry_price_senal=senal,
                     margin=20.0, notional=400.0, size=4.0, fee_entrada=0.0)
    repo.registrar_fill(pid, ts=MIN, reason=reason, fraction=1.0,
                        precio_referencia=salida_ref, precio=salida, comision=0.0,
                        precio_regla=precio_regla)
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
    assert "STOP" in bloque
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
    assert "STOP" in bloque
    assert "+100.0 bps" in bloque


def test_una_salida_sin_precio_regla_no_se_promedia(repo):
    # EXTREME por temporizador cierra a mercado adrede: no hay nivel
    # prometido contra el que medir, así que el "+0.0 bps" que daría medirlo
    # contra su propia referencia no debe aparecer como si fuera un dato
    # medido -se lista aparte, sin promediarse con el resto de motivos.
    _trade(repo, salida_ref=110.0, salida=100.0, reason=ExitReason.EXTREME,
          precio_regla=None)
    bloque = format_bloque_ejecucion(repo, "paper", descartes={}, cierres_tardios=0)
    assert "Desvio de salida por motivo:\n  sin datos (n=0)" in bloque
    assert "Salidas a mercado sin nivel de referencia (no promediadas):" in bloque
    assert "EXTREME      n=1" in bloque


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


# --- Bloque de modo real (Task 11) -----------------------------------------


def test_en_paper_no_aparece_el_bloque_de_modo_real(repo):
    # Restriccion del brief: en paper el bloque no cambia EN ABSOLUTO -ni
    # siquiera para añadir una linea nueva-, porque hay tests (los de
    # arriba) que fijan el formato actual carácter a carácter.
    _trade(repo)
    bloque = format_bloque_ejecucion(
        repo, "paper", descartes={}, cierres_tardios=0, saldo_real=850.0)
    assert "Modo real" not in bloque


def test_un_modo_desconocido_no_dispara_el_bloque_de_modo_real(repo):
    # Hallazgo de revision: antes se comprobaba `modo != "paper"`, asi que
    # un typo (`--modo pape`) imprimia el bloque entero con todo a cero en
    # vez de comportarse como el "paper" que probablemente se queria decir.
    # Ahora se comprueba pertenencia explicita a los modos reales de
    # bot/modo.py.
    bloque = format_bloque_ejecucion(
        repo, "pape", descartes={}, cierres_tardios=0, saldo_real=850.0)
    assert "Modo real" not in bloque


def test_en_real_sin_saldo_persistido_no_finge_una_diferencia(repo):
    repo.set_equity_inicial("real", 1000.0)
    bloque = format_bloque_ejecucion(
        repo, "real", descartes={}, cierres_tardios=0, saldo_real=None)
    assert "Modo real:" in bloque
    assert "sin dato todavia" in bloque
    assert "Diferencia" not in bloque


def test_en_real_con_saldo_muestra_la_diferencia_con_el_equity_calculado(repo):
    # `_trade` abre siempre en "paper" (ver su definición); aquí hace falta
    # una posición cerrada en "real" para que `repo.equity("real")` no se
    # quede en el inicial, así que se abre y cierra a mano.
    repo.set_equity_inicial("real", 1000.0)
    pid = repo.abrir(modo="real", symbol="A", direction=Direction.LONG,
                     entry_ts=0, entry_price=100.0, entry_price_senal=100.0,
                     margin=20.0, notional=400.0, size=4.0, fee_entrada=0.0)
    repo.registrar_fill(pid, ts=MIN, reason=ExitReason.STOP, fraction=1.0,
                        precio_referencia=110.0, precio=110.0, comision=0.0,
                        precio_regla=110.0)
    repo.cerrar(pid, close_ts=MIN, pnl=10.0, fees=0.0, max_rank=4)
    # equity calculado: 1000 (inicial) + 10 (pnl) = 1010
    bloque = format_bloque_ejecucion(
        repo, "real", descartes={}, cierres_tardios=0, saldo_real=1005.0)
    assert "Saldo real: 1005.00" in bloque
    assert "Equity calculado: 1010.00" in bloque
    assert "Diferencia (funding, comisiones no modeladas, redondeos): -5.00" in bloque


def test_en_real_los_cierres_de_sondeo_y_reconciliacion_se_cuentan_por_separado(repo):
    # Son dos caminos que detectan lo mismo -el exchange cerro por su cuenta-
    # en momentos distintos (con el bot vivo, o al arrancar tras una caida):
    # deben aparecer en lineas distintas, no sumados en un solo numero.
    repo.set_equity_inicial("real", 1000.0)
    repo.incrementar_contador("real", "cierres detectados por sondeo", 2)
    repo.incrementar_contador("real", "posiciones cerradas en el exchange", 1)
    bloque = format_bloque_ejecucion(
        repo, "real", descartes={}, cierres_tardios=0, saldo_real=1000.0)
    assert "Cierres ejecutados por el exchange (sondeo en vivo): 2" in bloque
    assert "Cierres ejecutados por el exchange (detectados al arrancar): 1" in bloque


def test_en_real_muestra_posiciones_ajenas_vetados_y_frenos(repo):
    repo.set_equity_inicial("real", 1000.0)
    repo.incrementar_contador("real", "posiciones ajenas", 2)
    repo.incrementar_contador("real", "simbolo vetado", 5)
    repo.incrementar_contador("real", "config cuenta", 4)
    repo.incrementar_contador("real", "perdida diaria", 3)
    repo.incrementar_contador("real", "parada de emergencia", 1)
    bloque = format_bloque_ejecucion(
        repo, "real", descartes={}, cierres_tardios=0, saldo_real=1000.0)
    assert "Posiciones ajenas detectadas: 2" in bloque
    # Dos lineas distintas a proposito (hallazgo de revision): un simbolo
    # vetado por posicion ajena y uno vetado por config de cuenta piden
    # acciones opuestas del operador, y sumarlas escondería cual hace falta.
    assert "Simbolos vetados (posicion ajena): 5" in bloque
    assert "Simbolos vetados (config de cuenta): 4" in bloque
    assert "Freno perdida diaria activado: 3 veces" in bloque
    assert "Freno parada de emergencia activado: 1 veces" in bloque


def test_en_real_lectura_tambien_aparece_el_bloque(repo):
    # "real_lectura" tambien es dinero conectado de verdad (solo que sin
    # mandar ordenes): el bloque debe aparecer igual que en "real", no solo
    # en el modo exacto "real".
    repo.set_equity_inicial("real_lectura", 1000.0)
    bloque = format_bloque_ejecucion(
        repo, "real_lectura", descartes={}, cierres_tardios=0, saldo_real=1000.0)
    assert "Modo real:" in bloque
