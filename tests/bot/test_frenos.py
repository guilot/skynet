"""Los frenos manuales: pérdida diaria máxima y parada de emergencia.

El caso que de verdad importa es la persistencia del saldo de referencia
(`test_la_referencia_del_dia_persiste_tras_un_reinicio`): si viviera solo en
memoria, un `Frenos` nuevo tras un reinicio bajo `Restart=always` recalcularía
la referencia sobre el saldo YA castigado -el mismo día en que el freno está
saltando- y el bot volvería a operar justo cuando no debe.
"""
import os
from datetime import datetime, timezone

import pytest

from scanner_volumen.bot.frenos import (
    MOTIVO_PARADA_EMERGENCIA, MOTIVO_PERDIDA_DIARIA, MOTIVO_SALDO_NO_FIABLE,
    Frenos,
)
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.config import BotConfig
from scanner_volumen.models import Direction
from scanner_volumen.storage.db import open_db

MIN = 60_000
# Un instante cualquiera de un día UTC, y el mismo instante del día
# siguiente -para probar que la referencia se renueva al cruzar la
# medianoche UTC, no la del reloj local.
DIA_1 = 1_700_000_000_000
DIA_2 = DIA_1 + 24 * 60 * 60 * 1000


def _dia(ts_ms: int) -> str:
    """Mismo cálculo que el `_dia_utc` privado de `frenos.py` -se
    reimplementa aquí en vez de importarlo para no acoplar el test a un
    símbolo privado del módulo bajo prueba."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _cfg(tmp_path, perdida_diaria_max: float = 0.10) -> BotConfig:
    return _cfg_con_fichero(tmp_path / "parar_bot", perdida_diaria_max)


def _cfg_con_fichero(fichero_parada, perdida_diaria_max: float = 0.10) -> BotConfig:
    return BotConfig(
        enabled=True, modo="paper", equity_inicial=1000.0,
        desvio_max_entrada=0.0, perdida_diaria_max=perdida_diaria_max,
        fichero_parada=str(fichero_parada),
    )


@pytest.fixture
def repo(tmp_path):
    conn = open_db(tmp_path / "scanner.db")
    r = BotRepo(conn)
    r.set_equity_inicial("paper", 1000.0)
    yield r
    conn.close()


def _perder(repo: BotRepo, monto: float, ts: int = DIA_1) -> None:
    """Cierra una posición con pérdida `monto`, bajando el equity derivado
    (`equity_inicial + Σ pnl de las cerradas`, ver `BotRepo.equity`)."""
    pid = repo.abrir(
        modo="paper", symbol="A", direction=Direction.LONG, entry_ts=ts,
        entry_price=100.0, entry_price_senal=100.0, margin=20.0,
        notional=400.0, size=4.0, fee_entrada=0.0,
    )
    repo.cerrar(pid, close_ts=ts, pnl=-monto, fees=0.0, max_rank=0)


def test_sin_perdidas_puede_abrir_devuelve_none(repo, tmp_path):
    frenos = Frenos(_cfg(tmp_path), repo, "paper")
    assert frenos.puede_abrir(DIA_1) is None


def test_superada_la_perdida_diaria_bloquea(repo, tmp_path):
    frenos = Frenos(_cfg(tmp_path), repo, "paper")
    # primera consulta del día: fija la referencia en el equity actual (1000)
    assert frenos.puede_abrir(DIA_1) is None
    _perder(repo, 150.0)  # equity baja a 850: 15% de pérdida > 10% del límite
    assert frenos.puede_abrir(DIA_1 + MIN) == MOTIVO_PERDIDA_DIARIA


def test_la_referencia_del_dia_persiste_tras_un_reinicio(tmp_path):
    """El test que importa de verdad. Simula el reinicio como lo vería el
    proceso real bajo `Restart=always`: se cierra la conexión original y se
    REABRE la misma base de datos en disco (`open_db` sobre el mismo
    fichero), con un `BotRepo` y un `Frenos` construidos desde cero -no se
    reutiliza ni el objeto `Frenos`, ni el `BotRepo`, ni la conexión-. El
    freno debe seguir activo, porque la referencia vive en `bot_meta`, en
    el fichero, no en un atributo de ninguna instancia en RAM."""
    ruta_db = tmp_path / "scanner.db"
    cfg = _cfg(tmp_path)

    conn = open_db(ruta_db)
    repo = BotRepo(conn)
    repo.set_equity_inicial("paper", 1000.0)
    frenos = Frenos(cfg, repo, "paper")
    frenos.puede_abrir(DIA_1)  # fija la referencia en 1000
    _perder(repo, 150.0)  # equity real: 850
    assert frenos.puede_abrir(DIA_1 + MIN) == MOTIVO_PERDIDA_DIARIA
    conn.close()

    # "reinicio": conexión, repositorio y Frenos construidos desde cero
    # sobre el MISMO fichero en disco.
    conn_reiniciada = open_db(ruta_db)
    try:
        repo_reiniciado = BotRepo(conn_reiniciada)
        frenos_reiniciado = Frenos(cfg, repo_reiniciado, "paper")
        assert (frenos_reiniciado.puede_abrir(DIA_1 + 2 * MIN)
                == MOTIVO_PERDIDA_DIARIA)
    finally:
        conn_reiniciada.close()


def test_registrar_saldo_del_dia_fija_solo_la_primera_vez(repo, tmp_path):
    """Una segunda llamada el mismo día UTC no debe pisar la referencia ya
    fijada -si lo hiciera, el freno se podría "resetear" en caliente
    simplemente volviendo a registrar el saldo ya castigado, exactamente el
    escenario que el freno existe para impedir."""
    frenos = Frenos(_cfg(tmp_path), repo, "paper")
    frenos.registrar_saldo_del_dia(DIA_1, 1000.0)
    frenos.registrar_saldo_del_dia(DIA_1 + MIN, 500.0)  # no debe pisar 1000
    _perder(repo, 100.0)  # equity real: 900 -> 10% de pérdida sobre 1000
    assert frenos.puede_abrir(DIA_1 + 2 * MIN) == MOTIVO_PERDIDA_DIARIA


def test_registrar_saldo_del_dia_no_tiene_efecto_si_puede_abrir_ya_consulto(
    repo, tmp_path,
):
    """Documenta el contrato de orden entre los dos métodos (ver los
    docstrings de `Frenos.puede_abrir` y `Frenos.registrar_saldo_del_dia`):
    `registrar_saldo_del_dia` debe llamarse ANTES de la primera consulta de
    `puede_abrir` del día. Si `puede_abrir` corre primero, ya fija la
    referencia con el equity derivado del bot, y una llamada posterior a
    `registrar_saldo_del_dia` -aunque traiga un saldo distinto, como haría
    la Task 13 con el saldo real del exchange en modo real- NO TIENE
    NINGÚN EFECTO. Si el cableado futuro invierte el orden, este test es
    el que lo delata."""
    frenos = Frenos(_cfg(tmp_path), repo, "paper")
    assert frenos.puede_abrir(DIA_1) is None  # fija la referencia en 1000 (equity)
    frenos.registrar_saldo_del_dia(DIA_1 + MIN, 500.0)  # llega tarde: sin efecto
    _perder(repo, 100.0)  # equity real: 900 -> 10% de perdida sobre la referencia (1000)
    assert frenos.puede_abrir(DIA_1 + 2 * MIN) == MOTIVO_PERDIDA_DIARIA
    # si `registrar_saldo_del_dia` hubiera pisado la referencia con 500, la
    # cuenta sería (500 - 900) / 500 < 0 y el freno NO se activaría.


def test_al_cambiar_de_dia_utc_la_referencia_se_renueva_y_el_freno_se_libera(
    repo, tmp_path,
):
    frenos = Frenos(_cfg(tmp_path), repo, "paper")
    frenos.puede_abrir(DIA_1)  # referencia día 1: 1000
    _perder(repo, 150.0)  # equity real: 850
    assert frenos.puede_abrir(DIA_1 + MIN) == MOTIVO_PERDIDA_DIARIA
    # cruza la medianoche UTC: nueva referencia = equity actual (850)
    assert frenos.puede_abrir(DIA_2) is None


def test_parada_de_emergencia_bloquea_y_borrar_el_fichero_reanuda(repo, tmp_path):
    """Crear el fichero corta, borrarlo reanuda -sin reiniciar nada- porque
    `puede_abrir` lo comprueba con `os.stat()` en cada llamada."""
    frenos = Frenos(_cfg(tmp_path), repo, "paper")
    fichero = tmp_path / "parar_bot"
    assert frenos.puede_abrir(DIA_1) is None

    fichero.write_text("")
    assert frenos.puede_abrir(DIA_1) == MOTIVO_PARADA_EMERGENCIA

    fichero.unlink()
    assert frenos.puede_abrir(DIA_1) is None


def test_parada_de_emergencia_frena_si_el_directorio_es_ilegible(repo, tmp_path):
    """El caso REAL de producción, no simulado: un directorio del fichero
    de parada vuelto ilegible (`chmod 000`) hace que `os.stat()` lance
    `PermissionError` al intentar resolver la ruta.

    Esto es justo lo que `Path.exists()` NO permite distinguir: en CPython
    (comprobado en 3.14 -`pathlib.Path.exists` delega en `os.path.exists`,
    que atrapa `(OSError, ValueError)` puertas adentro y devuelve `False`
    para cualquier fallo) un directorio ilegible se ve exactamente igual
    que "el fichero no existe", y `puede_abrir` habría dejado operar al bot
    en el único escenario de permisos que puede darse de verdad. Por eso
    `_parada_de_emergencia` usa `os.stat()` en vez de `Path.exists()` -este
    test es el que demuestra que la distinción importa de verdad, no solo
    en el papel."""
    if os.geteuid() == 0:
        pytest.skip("como root los permisos de fichero no se aplican")

    directorio = tmp_path / "protegido"
    directorio.mkdir()
    fichero = directorio / "parar_bot"
    fichero.write_text("")
    frenos = Frenos(_cfg_con_fichero(fichero), repo, "paper")

    os.chmod(directorio, 0o000)
    try:
        assert frenos.puede_abrir(DIA_1) == MOTIVO_PARADA_EMERGENCIA
    finally:
        # restaurar antes de terminar: si no, pytest no puede limpiar tmp_path
        os.chmod(directorio, 0o755)


def test_parada_de_emergencia_frena_ante_un_oserror_generico(
    repo, tmp_path, monkeypatch,
):
    """Complementa el test de arriba (que cubre `PermissionError` de
    verdad) con un `OSError` genérico -para que la rama "no se puede
    determinar" del código no dependa solo de un tipo concreto de fallo de
    permisos."""
    frenos = Frenos(_cfg(tmp_path), repo, "paper")

    def _revienta(ruta):
        raise OSError("simulado: fallo de E/S al comprobar la ruta")

    monkeypatch.setattr("scanner_volumen.bot.frenos.os.stat", _revienta)
    assert frenos.puede_abrir(DIA_1) == MOTIVO_PARADA_EMERGENCIA


# --- proveedor_saldo (Task 11, ronda de arreglo) ---------------------------


def test_sin_proveedor_el_freno_mide_solo_el_equity_contable(repo, tmp_path):
    # Regresion explicita del comportamiento SIN proveedor -el de paper, y
    # el de cualquier real mal cableado-: sigue siendo BotRepo.equity, tal
    # cual antes de este cambio.
    frenos = Frenos(_cfg(tmp_path), repo, "paper")
    assert frenos.puede_abrir(DIA_1) is None
    _perder(repo, 150.0)  # equity contable: 850 -> 15% > 10%
    assert frenos.puede_abrir(DIA_1 + MIN) == MOTIVO_PERDIDA_DIARIA


def test_con_proveedor_el_freno_mide_la_perdida_real_no_la_contable(repo, tmp_path):
    """Reproduce el hallazgo de revision: el equity CONTABLE (BD) puede
    caer menos que el saldo REAL del exchange -funding, comisiones no
    modeladas, redondeos, exactamente lo que el Step 1 de esta tarea existe
    para medir-. Sin `proveedor_saldo`, el freno mediria 4.5% (955 sobre
    1000, solo el pnl que ve la contabilidad) y NO saltaria, aunque el
    saldo real ya haya perdido un 7% -por encima del limite del 5%-. Con
    `proveedor_saldo`, el freno mide la cifra que de verdad gobierna el
    dinero."""
    repo.set_equity_inicial("real", 1000.0)
    cfg = _cfg(tmp_path, perdida_diaria_max=0.05)
    saldo_real = {"valor": 1000.0}
    frenos = Frenos(cfg, repo, "real", proveedor_saldo=lambda: saldo_real["valor"])
    assert frenos.puede_abrir(DIA_1) is None  # fija la referencia REAL: 1000

    # el equity contable baja a 955 (perdida contable: 4.5%, bajo el limite)
    pid = repo.abrir(
        modo="real", symbol="A", direction=Direction.LONG, entry_ts=DIA_1,
        entry_price=100.0, entry_price_senal=100.0, margin=20.0,
        notional=400.0, size=4.0, fee_entrada=0.0,
    )
    repo.cerrar(pid, close_ts=DIA_1, pnl=-45.0, fees=0.0, max_rank=0)
    # pero el saldo REAL cae a 930 (7% de perdida real, por encima del 5%)
    saldo_real["valor"] = 930.0
    assert frenos.puede_abrir(DIA_1 + MIN) == MOTIVO_PERDIDA_DIARIA


def test_con_proveedor_referencia_y_medida_salen_siempre_de_la_misma_fuente(
    repo, tmp_path,
):
    """El otro lado del mismo hallazgo: si referencia y medida pudieran
    salir de fuentes distintas (una real, otra contable), un desajuste
    entre las dos se leeria como una "ganancia" que nunca ocurrio -la
    "ganancia fantasma" que el coordinador reprodujo-. Con `proveedor_saldo`
    inyectado, las DOS puntas leen siempre de el: el equity contable
    (deliberadamente distinto aqui, 1000 sin tocar) no puede colarse en el
    calculo aunque el proveedor diga otra cosa."""
    repo.set_equity_inicial("real", 1000.0)  # equity contable: se queda en 1000
    cfg = _cfg(tmp_path, perdida_diaria_max=0.05)
    frenos = Frenos(cfg, repo, "real", proveedor_saldo=lambda: 850.0)
    assert frenos.puede_abrir(DIA_1) is None  # referencia: 850 (del proveedor, no 1000)
    # el saldo real no se ha movido -sigue en 850-: 0% de perdida real, pese
    # a que el equity contable (1000) diverja de la referencia.
    assert frenos.puede_abrir(DIA_1 + MIN) is None


@pytest.mark.parametrize("saldo_invalido", [float("nan"), 0.0, -50.0])
def test_un_saldo_invalido_del_proveedor_frena_sin_persistir_referencia(
    repo, tmp_path, saldo_invalido,
):
    """Hallazgo de revisión (ronda 2): antes de esta guarda, un `nan`
    colado en la PRIMERA consulta del día se persistía tal cual como
    referencia en `bot_meta`. A partir de ahí `referencia <= 0` daba
    `False`, `perdida` salía `nan`, y `nan >= tope` TAMBIÉN da `False` en
    Python -así que el freno de pérdida diaria quedaba desactivado el
    resto del día UTC, y sobrevivía a un reinicio porque la referencia
    envenenada ya estaba en disco. `0.0` y un negativo envenenan la
    jornada por el mismo camino (`referencia <= 0`). La guarda debe frenar
    (no dejar pasar, "ante la duda se frena") Y no escribir nada en
    `bot_meta` con ese valor."""
    repo.set_equity_inicial("real", 1000.0)
    cfg = _cfg(tmp_path, perdida_diaria_max=0.05)
    frenos = Frenos(cfg, repo, "real", proveedor_saldo=lambda: saldo_invalido)
    assert frenos.puede_abrir(DIA_1) == MOTIVO_PERDIDA_DIARIA
    assert repo.saldo_dia("real", _dia(DIA_1)) is None  # nada persistido


def test_tras_un_saldo_invalido_una_consulta_posterior_valida_fija_bien_la_referencia(
    repo, tmp_path,
):
    """Complementa el test de arriba: la referencia envenenada no debe
    quedar "atascada" en ningún estado intermedio -la primera consulta
    válida del día, aunque llegue después de una inválida, tiene que fijar
    la referencia con normalidad, como si la consulta inválida no hubiera
    pasado."""
    repo.set_equity_inicial("real", 1000.0)
    cfg = _cfg(tmp_path, perdida_diaria_max=0.05)
    saldo = {"valor": float("nan")}
    frenos = Frenos(cfg, repo, "real", proveedor_saldo=lambda: saldo["valor"])
    assert frenos.puede_abrir(DIA_1) == MOTIVO_PERDIDA_DIARIA  # invalido: frena

    saldo["valor"] = 1000.0  # ahora si es valido
    assert frenos.puede_abrir(DIA_1 + MIN) is None  # fija la referencia: 1000
    assert repo.saldo_dia("real", _dia(DIA_1)) == pytest.approx(1000.0)

    saldo["valor"] = 900.0  # 10% de perdida sobre la referencia de 1000
    assert frenos.puede_abrir(DIA_1 + 2 * MIN) == MOTIVO_PERDIDA_DIARIA


@pytest.mark.parametrize("saldo_invalido", [float("nan"), 0.0, -50.0])
def test_registrar_saldo_del_dia_con_valor_invalido_no_persiste_nada(
    repo, tmp_path, saldo_invalido,
):
    frenos = Frenos(_cfg(tmp_path), repo, "paper")
    frenos.registrar_saldo_del_dia(DIA_1, saldo_invalido)
    assert repo.saldo_dia("paper", _dia(DIA_1)) is None


def test_la_parada_de_emergencia_no_impide_gobernar_lo_ya_abierto(repo, tmp_path):
    """Los frenos son responsabilidad exclusiva de `puede_abrir`: no exponen
    nada que pueda usarse para tocar la gestión de lo ya abierto. Este test
    documenta la regla -el cableado real en `on_tick` (Task 10, paso 4) es
    el que de verdad la hace cumplir- comprobando que `puede_abrir` activo no
    afecta en nada a `BotRepo.abiertas`, que sigue devolviendo lo que haya."""
    frenos = Frenos(_cfg(tmp_path), repo, "paper")
    pid = repo.abrir(
        modo="paper", symbol="A", direction=Direction.LONG, entry_ts=DIA_1,
        entry_price=100.0, entry_price_senal=100.0, margin=20.0,
        notional=400.0, size=4.0, fee_entrada=0.0,
    )
    (tmp_path / "parar_bot").write_text("")
    assert frenos.puede_abrir(DIA_1) == MOTIVO_PARADA_EMERGENCIA
    assert [f["id"] for f in repo.abiertas("paper")] == [pid]


# --- tercer freno: saldo no fiable (Task 13, ronda de revision) ---


def _cfg_frenos(tmp_path, perdida=0.10):
    return BotConfig(
        enabled=True, modo="real", equity_inicial=1000.0, desvio_max_entrada=0.0,
        perdida_diaria_max=perdida, fichero_parada=str(tmp_path / "no-existe"),
    )


def test_un_proveedor_que_lanza_frena_con_su_propio_motivo(tmp_path, repo, caplog):
    """El proveedor de saldo de los modos reales LANZA cuando no tiene una
    lectura real y reciente. Antes de esta ronda esa excepcion subia hasta el
    manejador generico del bucle evaluador: el bot dejaba de abrir -correcto-
    pero el unico rastro era un `log.exception` POR TICK y el informe no
    decia nada de por que se habia parado."""
    repo.set_equity_inicial("real", 1000.0)

    def proveedor_roto():
        raise ValueError("todavia no se ha observado ningun saldo real")

    frenos = Frenos(_cfg_frenos(tmp_path), repo, "real", proveedor_roto)

    with caplog.at_level("ERROR"):
        assert frenos.puede_abrir(0) == MOTIVO_SALDO_NO_FIABLE

    # y NO se persiste ninguna referencia del dia con un valor fantasma
    assert repo.saldo_dia("real", "1970-01-01") is None


def test_el_aviso_de_saldo_no_fiable_no_se_repite_en_cada_tick(tmp_path, repo, caplog):
    """Con la cadencia del evaluador (1 s), un corte de red de diez minutos
    serian ~600 mensajes identicos ahogando el log."""
    repo.set_equity_inicial("real", 1000.0)

    def proveedor_roto():
        raise ValueError("sin saldo")

    frenos = Frenos(_cfg_frenos(tmp_path), repo, "real", proveedor_roto)

    with caplog.at_level("ERROR"):
        for _ in range(5):
            assert frenos.puede_abrir(0) == MOTIVO_SALDO_NO_FIABLE

    avisos = [r for r in caplog.records if "saldo real fiable" in r.getMessage()]
    assert len(avisos) == 1


def test_el_freno_de_saldo_no_fiable_se_levanta_al_volver_el_saldo(tmp_path, repo):
    repo.set_equity_inicial("real", 1000.0)
    estado = {"roto": True}

    def proveedor(_estado=estado):
        if _estado["roto"]:
            raise ValueError("sin saldo")
        return 1000.0

    frenos = Frenos(_cfg_frenos(tmp_path), repo, "real", proveedor)
    assert frenos.puede_abrir(0) == MOTIVO_SALDO_NO_FIABLE

    estado["roto"] = False
    assert frenos.puede_abrir(0) is None
    # y ahora sí fija la referencia del día, con el saldo bueno
    assert repo.saldo_dia("real", "1970-01-01") == pytest.approx(1000.0)


def test_la_parada_de_emergencia_gana_al_saldo_no_fiable(tmp_path, repo):
    """El orden importa para quien lee el informe: si un humano ha accionado
    la parada, eso es lo que hay que contarle, no un problema de red."""
    repo.set_equity_inicial("real", 1000.0)
    parada = tmp_path / "parar_bot"
    parada.write_text("")
    cfg = BotConfig(
        enabled=True, modo="real", equity_inicial=1000.0, desvio_max_entrada=0.0,
        perdida_diaria_max=0.10, fichero_parada=str(parada),
    )

    def proveedor_roto():
        raise ValueError("sin saldo")

    frenos = Frenos(cfg, repo, "real", proveedor_roto)
    assert frenos.puede_abrir(0) == MOTIVO_PARADA_EMERGENCIA


def test_sin_perdida_diaria_activa_no_se_consulta_el_saldo(tmp_path, repo):
    """`paper`: el freno de perdida diaria no se evalua en absoluto -no se
    lee el saldo ni se persiste ninguna referencia-, para no dejar de abrir
    entradas que el backtest si abre y romper la comparacion. La parada de
    emergencia, en cambio, sigue viva (ver `test_main.py`)."""
    consultas = {"n": 0}

    def proveedor():
        consultas["n"] += 1
        return 1.0  # una perdida del 99.9% que deberia frenar si se evaluara

    cfg = BotConfig(
        enabled=True, modo="paper", equity_inicial=1000.0, desvio_max_entrada=0.0,
        perdida_diaria_max=0.10, fichero_parada=str(tmp_path / "no-existe"),
    )
    frenos = Frenos(cfg, repo, "paper", proveedor, perdida_diaria_activa=False)

    assert frenos.puede_abrir(0) is None
    assert consultas["n"] == 0
    assert repo.saldo_dia("paper", "1970-01-01") is None
