"""Tests unitarios de la guarda de seguridad (`seguridad.py`), NO del banco de
integración: no usan ninguna fixture de `conftest.py`, no necesitan claves y
corren siempre, dentro de la suite normal -son la prueba de que la guarda de
verdad discrimina, no una comprobación tautológica sobre sus propias
constantes.
"""
from __future__ import annotations

import pytest

from .seguridad import (
    PRODUCT_TYPE_DEMO,
    SYMBOL_DEMO,
    _verificar_entorno_de_simulacion,
)


def test_el_productType_y_simbolo_de_simulacion_pasan_la_guarda():
    _verificar_entorno_de_simulacion(PRODUCT_TYPE_DEMO, SYMBOL_DEMO)  # no lanza


def test_un_productType_de_produccion_aborta():
    with pytest.raises(RuntimeError, match="ABORTADO"):
        _verificar_entorno_de_simulacion("USDT-FUTURES", SYMBOL_DEMO)


def test_un_simbolo_sin_prefijo_S_aborta():
    with pytest.raises(RuntimeError, match="ABORTADO"):
        _verificar_entorno_de_simulacion(PRODUCT_TYPE_DEMO, "BTCUSDT")


def test_ambos_invalidos_a_la_vez_aborta_por_el_productType_primero():
    # Cualquiera de los dos motivos debe abortar; se fija el orden de
    # evaluacion (productType antes que simbolo) para que el mensaje de
    # error sea predecible si algun dia se rompen los dos a la vez.
    with pytest.raises(RuntimeError, match="productType"):
        _verificar_entorno_de_simulacion("USDT-FUTURES", "BTCUSDT")


def test_el_mensaje_de_error_no_confunde_los_dos_motivos():
    with pytest.raises(RuntimeError, match="simbolo") as exc_info:
        _verificar_entorno_de_simulacion(PRODUCT_TYPE_DEMO, "BTCUSDT")
    # el mensaje del motivo de simbolo no debe hablar de productType
    assert "productType" not in str(exc_info.value)
