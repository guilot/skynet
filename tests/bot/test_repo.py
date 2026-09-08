import pytest

from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.models import Direction
from scanner_volumen.storage.db import open_db
from scanner_volumen.strategy.model import ExitReason

MIN = 60_000


@pytest.fixture
def repo(tmp_path):
    conn = open_db(tmp_path / "scanner.db")
    yield BotRepo(conn)
    conn.close()


def _abrir(repo, symbol="A", ts=0, precio=100.0, margin=20.0):
    return repo.abrir(
        modo="paper", symbol=symbol, direction=Direction.LONG, entry_ts=ts,
        entry_price=precio, entry_price_senal=precio, margin=margin,
        notional=margin * 20, size=margin * 20 / precio, fee_entrada=0.24,
    )


def _reservar(repo, modo="paper", symbol="A", ts=0, precio=100.0, margin=20.0,
              client_oid="oid-test"):
    """Igual que `_abrir`, pero como lo hace `BotRunner._abrir` ANTES de
    mandar la orden: `confirmada=False` y con `client_oid`."""
    return repo.abrir(
        modo=modo, symbol=symbol, direction=Direction.LONG, entry_ts=ts,
        entry_price=precio, entry_price_senal=precio, margin=margin,
        notional=margin * 20, size=margin * 20 / precio, fee_entrada=0.0,
        client_oid=client_oid, confirmada=False,
    )


def test_abrir_devuelve_id_y_aparece_como_abierta(repo):
    pid = _abrir(repo)
    assert pid > 0
    abiertas = repo.abiertas("paper")
    assert len(abiertas) == 1
    assert abiertas[0]["symbol"] == "A"
    assert abiertas[0]["entry_price"] == pytest.approx(100.0)


def test_la_comision_de_entrada_se_persiste(repo):
    # la reconstruccion tras un reinicio la necesita para recomponer el PnL de
    # una posicion que sigue abierta
    _abrir(repo)
    assert repo.abiertas("paper")[0]["fee_entrada"] == pytest.approx(0.24)


def test_cerrar_saca_la_posicion_de_abiertas(repo):
    pid = _abrir(repo)
    repo.cerrar(pid, close_ts=MIN, pnl=12.0, fees=0.5, max_rank=3)
    assert repo.abiertas("paper") == []
    cerradas = repo.cerradas("paper")
    assert len(cerradas) == 1
    assert cerradas[0]["pnl"] == pytest.approx(12.0)


def test_cerradas_sin_limite_devuelve_todas_ascendente(repo):
    for i, symbol in enumerate(["A", "B", "C"]):
        pid = _abrir(repo, symbol=symbol, ts=i * MIN)
        repo.cerrar(pid, close_ts=(i + 1) * MIN, pnl=float(i), fees=0.0, max_rank=1)
    cerradas = repo.cerradas("paper")
    assert [f["symbol"] for f in cerradas] == ["A", "B", "C"]


def test_cerradas_con_limite_devuelve_las_mas_recientes_primero(repo):
    """El panel del dashboard pide `limite` para no traer el historial
    completo en cada sondeo (Ronda 1 de Task 10): la consulta debe recortar
    en SQL, no en Python, y devolver las últimas N en orden descendente."""
    for i, symbol in enumerate(["A", "B", "C", "D"]):
        pid = _abrir(repo, symbol=symbol, ts=i * MIN)
        repo.cerrar(pid, close_ts=(i + 1) * MIN, pnl=float(i), fees=0.0, max_rank=1)
    cerradas = repo.cerradas("paper", limite=2)
    assert [f["symbol"] for f in cerradas] == ["D", "C"]


def test_fills_se_guardan_y_se_leen_en_orden(repo):
    pid = _abrir(repo)
    repo.registrar_fill(pid, ts=MIN, reason=ExitReason.SCALE_HOT, fraction=0.33,
                        precio_referencia=110.0, precio=109.5, comision=0.2)
    repo.registrar_fill(pid, ts=2 * MIN, reason=ExitReason.EXTREME, fraction=0.67,
                        precio_referencia=130.0, precio=129.0, comision=0.4,
                        tardio=True)
    fills = repo.fills_de(pid)
    assert [f["reason"] for f in fills] == ["SCALE_HOT", "EXTREME"]
    assert fills[0]["precio"] == pytest.approx(109.5)
    assert fills[1]["tardio"] == 1


def test_equity_se_deriva_de_las_cerradas(repo):
    # el equity NO se guarda: se deriva, para que no pueda desincronizarse
    repo.set_equity_inicial("paper", 1000.0)
    assert repo.equity("paper") == pytest.approx(1000.0)
    pid = _abrir(repo)
    repo.cerrar(pid, close_ts=MIN, pnl=-150.0, fees=1.0, max_rank=1)
    assert repo.equity("paper") == pytest.approx(850.0)


def test_las_posiciones_abiertas_no_cuentan_en_el_equity(repo):
    repo.set_equity_inicial("paper", 1000.0)
    _abrir(repo)  # sigue abierta
    assert repo.equity("paper") == pytest.approx(1000.0)


def test_el_equity_inicial_persiste(repo):
    repo.set_equity_inicial("paper", 1000.0)
    assert repo.equity_inicial("paper", defecto=999.0) == pytest.approx(1000.0)


def test_equity_inicial_devuelve_el_defecto_si_no_hay_nada(repo):
    assert repo.equity_inicial("paper", defecto=1234.0) == pytest.approx(1234.0)


def test_el_equity_inicial_no_se_mezcla_entre_modos(repo):
    # el día que exista operativa "real", no debe arrancar sobre el capital
    # del "paper": cada modo tiene su propia fila en bot_meta.
    repo.set_equity_inicial("paper", 1000.0)
    repo.set_equity_inicial("real", 50.0)
    assert repo.equity_inicial("paper", defecto=0.0) == pytest.approx(1000.0)
    assert repo.equity_inicial("real", defecto=0.0) == pytest.approx(50.0)


def test_saldo_real_sin_dato_devuelve_none(repo):
    # a diferencia de equity_inicial, saldo_real no tiene un "defecto" que
    # tenga sentido inventar: None es la señal de "el proceso en vivo
    # todavia no ha persistido ninguno" que el informe necesita distinguir
    # de un saldo real de 0.
    assert repo.saldo_real("real") is None


def test_saldo_real_persiste(repo):
    repo.set_saldo_real("real", 850.0)
    assert repo.saldo_real("real") == pytest.approx(850.0)


def test_saldo_real_se_puede_actualizar(repo):
    # cada tick en real sobreescribe el anterior: el informe solo quiere el
    # ULTIMO saldo conocido, no un historial.
    repo.set_saldo_real("real", 850.0)
    repo.set_saldo_real("real", 830.0)
    assert repo.saldo_real("real") == pytest.approx(830.0)


def test_saldo_real_no_se_mezcla_entre_modos(repo):
    # "paper" nunca deberia tener un saldo_real persistido, pero si algo lo
    # hiciera, no debe contaminar la lectura de "real".
    repo.set_saldo_real("real", 850.0)
    assert repo.saldo_real("paper") is None


def test_los_modos_no_se_mezclan(repo):
    repo.set_equity_inicial("paper", 1000.0)
    pid = repo.abrir(modo="real", symbol="B", direction=Direction.LONG, entry_ts=0,
                     entry_price=100.0, entry_price_senal=100.0, margin=20.0,
                     notional=400.0, size=4.0, fee_entrada=0.0)
    repo.cerrar(pid, close_ts=MIN, pnl=500.0, fees=0.0, max_rank=4)
    assert repo.equity("paper") == pytest.approx(1000.0)  # el trade real no suma
    assert repo.abiertas("paper") == []
    # H.3: la vía por la que el modo llega al informe es `cerradas`, no solo
    # `equity`/`abiertas` -sin esta comprobación, un filtro de modo roto en
    # `cerradas` (p. ej. un WHERE olvidado) habría pasado desapercibido.
    assert repo.cerradas("paper") == []
    assert len(repo.cerradas("real")) == 1
    assert repo.cerradas("real")[0]["pnl"] == pytest.approx(500.0)


def test_registrar_fill_persiste_precio_regla(repo):
    pid = _abrir(repo)
    repo.registrar_fill(pid, ts=MIN, reason=ExitReason.STOP, fraction=1.0,
                        precio_referencia=97.5, precio=97.0, comision=0.1,
                        precio_regla=97.5)
    assert repo.fills_de(pid)[0]["precio_regla"] == pytest.approx(97.5)


def test_registrar_fill_sin_precio_regla_queda_nulo(repo):
    # motivos como EXTREME cierran a mercado al vencer el temporizador: no
    # hay nivel prometido contra el que medir, y NULL debe distinguirse de 0.
    pid = _abrir(repo)
    repo.registrar_fill(pid, ts=MIN, reason=ExitReason.EXTREME, fraction=1.0,
                        precio_referencia=100.0, precio=100.0, comision=0.0)
    assert repo.fills_de(pid)[0]["precio_regla"] is None


def test_marcar_degradada_persiste_el_flag(repo):
    pid = _abrir(repo)
    assert repo.abiertas("paper")[0]["degradada"] == 0
    repo.marcar_degradada(pid)
    assert repo.abiertas("paper")[0]["degradada"] == 1


def test_por_client_oid_encuentra_la_fila_reservada(repo):
    pid = _reservar(repo, client_oid="oid-1")
    fila = repo.por_client_oid("paper", "oid-1")
    assert fila is not None
    assert fila["id"] == pid
    assert fila["symbol"] == "A"
    assert fila["confirmada"] == 0


def test_por_client_oid_devuelve_none_si_no_existe(repo):
    assert repo.por_client_oid("paper", "oid-inexistente") is None


def test_por_client_oid_no_mezcla_modos(repo):
    _reservar(repo, modo="real", symbol="B", client_oid="oid-2")
    assert repo.por_client_oid("paper", "oid-2") is None
    assert repo.por_client_oid("real", "oid-2") is not None


def test_confirmar_apertura_actualiza_precio_tamano_comision_y_marca_confirmada(repo):
    pid = _reservar(repo, precio=100.0, client_oid="oid-3")
    assert repo.abiertas("paper")[0]["confirmada"] == 0

    # el broker devolvio un precio y una cantidad distintos de los
    # provisionales con los que se reservo la fila
    repo.confirmar_apertura(pid, entry_price=101.5, size=3.94, fee_entrada=0.24)

    fila = repo.abiertas("paper")[0]
    assert fila["entry_price"] == pytest.approx(101.5)
    assert fila["size"] == pytest.approx(3.94)
    assert fila["fee_entrada"] == pytest.approx(0.24)
    assert fila["confirmada"] == 1


def test_reservadas_sin_confirmar_deja_de_devolver_la_fila_al_confirmarla(repo):
    # esta transicion (aparece -> desaparece) es exactamente la que usara la
    # reconciliacion de arranque (Task 8) para distinguir una orden huerfana
    # -mandada pero nunca confirmada- de una posicion normal.
    pid = _reservar(repo, client_oid="oid-4")

    reservadas = repo.reservadas_sin_confirmar("paper")
    assert len(reservadas) == 1
    assert reservadas[0]["id"] == pid
    assert reservadas[0]["client_oid"] == "oid-4"

    repo.confirmar_apertura(pid, entry_price=100.5, size=4.0, fee_entrada=0.1)

    assert repo.reservadas_sin_confirmar("paper") == []


def test_reservadas_sin_confirmar_no_mezcla_modos(repo):
    _reservar(repo, modo="real", symbol="B", client_oid="oid-5")
    assert repo.reservadas_sin_confirmar("paper") == []
    assert len(repo.reservadas_sin_confirmar("real")) == 1


def test_reservadas_sin_confirmar_no_incluye_posiciones_ya_confirmadas(repo):
    # `abrir()` sin `confirmada=False` (el camino que usan el resto de
    # llamadores) debe quedar fuera desde el principio, no solo tras un
    # `confirmar_apertura` explicito.
    _abrir(repo)
    assert repo.reservadas_sin_confirmar("paper") == []


def test_incrementar_contador_acumula_por_modo_y_clave(repo):
    repo.incrementar_contador("paper", "NEUTRAL")
    repo.incrementar_contador("paper", "NEUTRAL")
    repo.incrementar_contador("paper", "transiciones", cantidad=5)
    repo.incrementar_contador("real", "NEUTRAL")  # no debe mezclarse
    assert repo.contadores("paper") == {"NEUTRAL": 2, "transiciones": 5}
    assert repo.contadores("real") == {"NEUTRAL": 1}


def test_fijar_maximo_solo_sube(repo):
    repo.fijar_maximo("paper", "max_concurrentes", 3)
    repo.fijar_maximo("paper", "max_concurrentes", 1)  # no debe bajar
    assert repo.contadores("paper")["max_concurrentes"] == 3
    repo.fijar_maximo("paper", "max_concurrentes", 7)  # sí debe subir
    assert repo.contadores("paper")["max_concurrentes"] == 7


def test_contadores_de_un_modo_sin_datos_es_vacio(repo):
    assert repo.contadores("paper") == {}


def test_arrancado_ms_sin_dato_devuelve_el_defecto(repo):
    assert repo.arrancado_ms() is None
    assert repo.arrancado_ms(defecto=1_000) == 1_000


def test_arrancado_ms_persiste(repo):
    repo.set_arrancado_ms(1_000)
    assert repo.arrancado_ms() == 1_000


def test_arrancado_ms_respeta_el_ya_guardado_en_arranques_posteriores(repo):
    # el idioma que usa `scanner_volumen/__main__.py`:
    # `set_arrancado_ms(arrancado_ms(defecto=ahora))`. El primer arranque
    # fija el valor; uno posterior, con un `ahora` distinto, no debe pisarlo.
    repo.set_arrancado_ms(repo.arrancado_ms(defecto=1_000))
    repo.set_arrancado_ms(repo.arrancado_ms(defecto=9_999))
    assert repo.arrancado_ms() == 1_000
