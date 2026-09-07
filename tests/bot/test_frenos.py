"""Los frenos manuales: pérdida diaria máxima y parada de emergencia.

El caso que de verdad importa es la persistencia del saldo de referencia
(`test_la_referencia_del_dia_persiste_tras_un_reinicio`): si viviera solo en
memoria, un `Frenos` nuevo tras un reinicio bajo `Restart=always` recalcularía
la referencia sobre el saldo YA castigado -el mismo día en que el freno está
saltando- y el bot volvería a operar justo cuando no debe.
"""
import pytest

from scanner_volumen.bot.frenos import (
    MOTIVO_PARADA_EMERGENCIA, MOTIVO_PERDIDA_DIARIA, Frenos,
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


def _cfg(tmp_path, perdida_diaria_max: float = 0.10) -> BotConfig:
    return BotConfig(
        enabled=True, modo="paper", equity_inicial=1000.0,
        desvio_max_entrada=0.0, perdida_diaria_max=perdida_diaria_max,
        fichero_parada=str(tmp_path / "parar_bot"),
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


def test_la_referencia_del_dia_persiste_tras_un_reinicio(repo, tmp_path):
    """El test que importa de verdad: un `Frenos` NUEVO sobre el MISMO
    repositorio (nunca se reabre la conexión ni se reconstruye el `Frenos`
    original en memoria) debe seguir viendo el freno activo, porque la
    referencia vive en `bot_meta`, no en un atributo de la instancia."""
    cfg = _cfg(tmp_path)
    frenos = Frenos(cfg, repo, "paper")
    frenos.puede_abrir(DIA_1)  # fija la referencia en 1000
    _perder(repo, 150.0)  # equity real: 850
    assert frenos.puede_abrir(DIA_1 + MIN) == MOTIVO_PERDIDA_DIARIA

    # "reinicio": una instancia nueva, no la misma con estado en RAM
    frenos_reiniciado = Frenos(cfg, repo, "paper")
    assert frenos_reiniciado.puede_abrir(DIA_1 + 2 * MIN) == MOTIVO_PERDIDA_DIARIA


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
    `puede_abrir` lo comprueba con `Path.exists()` en cada llamada."""
    frenos = Frenos(_cfg(tmp_path), repo, "paper")
    fichero = tmp_path / "parar_bot"
    assert frenos.puede_abrir(DIA_1) is None

    fichero.write_text("")
    assert frenos.puede_abrir(DIA_1) == MOTIVO_PARADA_EMERGENCIA

    fichero.unlink()
    assert frenos.puede_abrir(DIA_1) is None


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
