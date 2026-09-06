"""Tests del broker de paper."""
import ast
from pathlib import Path

import pytest

from scanner_volumen.bot.broker import PaperBroker
from scanner_volumen.models import Direction
from scanner_volumen.strategy.model import StrategyParams

MIN = 60_000


async def test_abrir_rellena_al_precio_de_mercado():
    broker = PaperBroker(StrategyParams(comision_taker=0.0))
    orden = await broker.abrir(symbol="A", direction=Direction.LONG,
                               notional=400.0, precio_mercado=100.0, ts=MIN,
                               client_oid="oid-test")
    assert orden.precio == pytest.approx(100.0)
    assert orden.cantidad == pytest.approx(4.0)   # 400 / 100
    assert orden.ts == MIN


async def test_abrir_cobra_comision_sobre_el_nocional():
    broker = PaperBroker(StrategyParams(comision_taker=0.0006))
    orden = await broker.abrir(symbol="A", direction=Direction.LONG,
                               notional=400.0, precio_mercado=100.0, ts=0,
                               client_oid="oid-test")
    assert orden.comision == pytest.approx(0.24)  # 0.0006 * 400


async def test_cerrar_cobra_comision_sobre_lo_cerrado():
    broker = PaperBroker(StrategyParams(comision_taker=0.0006))
    orden = await broker.cerrar(symbol="A", direction=Direction.LONG,
                                cantidad=2.0, precio_mercado=110.0, ts=MIN)
    assert orden.precio == pytest.approx(110.0)
    assert orden.cantidad == pytest.approx(2.0)
    assert orden.comision == pytest.approx(0.132)  # 0.0006 * 2 * 110


@pytest.mark.parametrize("metodo,precio", [
    ("abrir", 0),
    ("abrir", -1),
    ("cerrar", 0),
    ("cerrar", -1),
])
async def test_rechaza_precio_no_positivo(metodo, precio):
    """abrir() y cerrar() deben fallar si precio_mercado no es estrictamente positivo."""
    broker = PaperBroker(StrategyParams())
    with pytest.raises(ValueError, match="precio_mercado debe ser estrictamente positivo"):
        if metodo == "abrir":
            await broker.abrir(symbol="A", direction=Direction.LONG,
                               notional=400.0, precio_mercado=precio, ts=MIN,
                               client_oid="oid-test")
        else:
            await broker.cerrar(symbol="A", direction=Direction.LONG,
                                cantidad=2.0, precio_mercado=precio, ts=MIN)


async def test_el_paper_broker_registra_el_stop_colocado():
    broker = PaperBroker(StrategyParams())
    sid = await broker.colocar_stop(symbol="A", direction=Direction.LONG,
                                    cantidad=4.0, precio_disparo=97.5,
                                    client_oid="oid-1")
    assert sid
    assert broker.stops_vivos()["A"].precio_disparo == pytest.approx(97.5)


async def test_mover_el_stop_cambia_el_precio_y_conserva_uno_solo():
    broker = PaperBroker(StrategyParams())
    sid = await broker.colocar_stop(symbol="A", direction=Direction.LONG,
                                    cantidad=4.0, precio_disparo=97.5,
                                    client_oid="oid-1")
    await broker.mover_stop(symbol="A", stop_id=sid, precio_disparo=100.0)
    assert len(broker.stops_vivos()) == 1
    assert broker.stops_vivos()["A"].precio_disparo == pytest.approx(100.0)


async def test_cancelar_el_stop_lo_elimina():
    broker = PaperBroker(StrategyParams())
    sid = await broker.colocar_stop(symbol="A", direction=Direction.LONG,
                                    cantidad=4.0, precio_disparo=97.5,
                                    client_oid="oid-1")
    await broker.cancelar_stop(symbol="A", stop_id=sid)
    assert broker.stops_vivos() == {}


async def test_cancelar_un_stop_inexistente_no_revienta():
    # en real puede haberse ejecutado ya; cancelarlo debe ser idempotente
    broker = PaperBroker(StrategyParams())
    await broker.cancelar_stop(symbol="A", stop_id="no-existe")


def test_paper_broker_no_importa_clientes_de_red():
    """PaperBroker no debe importar clientes HTTP, WebSocket ni módulos de Bitget.

    Se verifica sobre el árbol de sintaxis para detectar imports accidentales
    sin necesidad de que el módulo se cargue ni se ejecute.
    """
    modulos_prohibidos = {
        "httpx", "websockets", "urllib", "requests", "aiohttp",
        "scanner_volumen.bitget"
    }

    # Obtener el árbol de sintaxis del módulo broker.py
    ruta_broker = Path(__file__).resolve().parents[2] / "scanner_volumen" / "bot" / "broker.py"
    arbol = ast.parse(ruta_broker.read_text(encoding="utf-8"), filename=str(ruta_broker))

    # Extraer todos los módulos importados
    imports_encontrados: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            for alias in nodo.names:
                imports_encontrados.add(alias.name)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            imports_encontrados.add(nodo.module)

    # Buscar módulos prohibidos (verificar si alguno es raíz de un import)
    ofensores: list[str] = []
    for imp in imports_encontrados:
        for prohibido in modulos_prohibidos:
            if imp.startswith(prohibido):
                ofensores.append(imp)
                break

    assert not ofensores, (
        f"broker.py no debe importar clientes de red ni módulos de Bitget. "
        f"Encontrados: {ofensores}"
    )
