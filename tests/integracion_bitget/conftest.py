"""Fixtures del banco de pruebas contra la simulación de Bitget (Task 12).

Ver el docstring de `test_ciclo_completo.py` para las instrucciones completas
de cómo obtener las claves de demo trading y ejecutar este banco.

Este módulo es el único punto de construcción de `BitgetPrivate`/
`BitgetBroker` reales de todo el banco: cualquier test que use las fixtures
`privado` o `broker` pasa, sin excepción, por `_verificar_entorno_de_
simulacion` (`seguridad.py`) antes de que exista un cliente autenticado.
"""
from __future__ import annotations

import os

import httpx
import pytest

from scanner_volumen.bitget.private import BitgetPrivate
from scanner_volumen.bot.bitget_broker import BitgetBroker
from scanner_volumen.strategy.model import StrategyParams

from .seguridad import (
    PRODUCT_TYPE_DEMO,
    SYMBOL_DEMO,
    VAR_KEY,
    VAR_PASSPHRASE,
    VAR_SECRET,
    _verificar_entorno_de_simulacion,
)

# Límite de peticiones conservador: este banco corre a mano, de vez en
# cuando, no en cada cambio -no hay ninguna prisa que justifique arriesgar
# el rate limit de Bitget.
RATE_LIMIT = 5.0


@pytest.fixture(scope="session")
def credenciales_demo() -> tuple[str, str, str]:
    """Lee las tres variables de entorno de demo y SALTA (no falla) el banco
    entero si falta alguna -es la máquina de hoy, que no tiene claves-."""
    faltan = [v for v in (VAR_KEY, VAR_SECRET, VAR_PASSPHRASE) if not os.environ.get(v)]
    if faltan:
        pytest.skip(
            "Banco de pruebas contra la simulacion de Bitget saltado: "
            f"faltan las variables de entorno {', '.join(faltan)}. Son las "
            "claves de una cuenta DEMO (fondos virtuales, no dinero real) -"
            "ver el docstring de tests/integracion_bitget/test_ciclo_completo.py "
            "para como obtenerlas en la interfaz de Bitget y configurarlas."
        )
    return (
        os.environ[VAR_KEY],
        os.environ[VAR_SECRET],
        os.environ[VAR_PASSPHRASE],
    )


@pytest.fixture
async def privado(credenciales_demo: tuple[str, str, str]):
    """Cliente autenticado apuntando, de forma verificada, a la simulación.

    `_verificar_entorno_de_simulacion` corre aquí, ANTES de construir nada,
    con los valores literales de `seguridad.py` -nunca con una variable que
    un test pudiera haber reasignado-. Si algún día alguien cambia
    `PRODUCT_TYPE_DEMO`/`SYMBOL_DEMO` a un valor que no sea de simulación,
    esta llamada aborta todo el banco antes de la primera petición HTTP.
    """
    _verificar_entorno_de_simulacion(PRODUCT_TYPE_DEMO, SYMBOL_DEMO)
    api_key, api_secret, passphrase = credenciales_demo
    async with httpx.AsyncClient(timeout=10.0) as cliente_http:
        yield BitgetPrivate(
            PRODUCT_TYPE_DEMO,
            RATE_LIMIT,
            cliente_http,
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase,
        )


@pytest.fixture
def broker(privado: BitgetPrivate) -> BitgetBroker:
    """`BitgetBroker` sobre el mismo cliente ya verificado por `privado`."""
    return BitgetBroker(privado, StrategyParams())
