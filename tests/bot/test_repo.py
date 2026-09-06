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
