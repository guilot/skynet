"""Sondeos dedicados a los supuestos de la API de Bitget que el camino feliz
del ciclo completo (`test_ciclo_completo.py`) NO ejercita por sí solo (Step 3
del brief de la Task 12).

De los nueve supuestos listados en `task-6-report.md` y en la sección final
de `task-11-report.md`, cuatro ya quedan bajo sospecha vigilada dentro de
`test_ciclo_completo.py` (envueltos en `bajo_sospecha`, con el paso exacto
donde se ejercitan):

- Supuesto 2 (`holdSide` en `place-tpsl-order`) — paso 4 del ciclo.
- Supuesto 3 (`planType="loss_plan"`) — paso 4 del ciclo.
- Supuesto 4 (forma de la respuesta de `/order/fills`) — pasos 2 y 6.
- Supuesto 5 (si `modify-tpsl-order` conserva el `orderId`) — paso 5.
- Supuesto 9 (`accountEquity`/`unrealizedPL`) — paso 1.

Este fichero cubre los que necesitan un sondeo PROPIO, deliberado, porque el
camino feliz nunca los pone a prueba:

- Supuesto 1 (el más arriesgado, Task 6): qué `code` exacto devuelve Bitget
  al cancelar un plan order que nunca existió. `BitgetBroker.cancelar_stop`
  trata CUALQUIER `RuntimeError` como "ya no existe"; el camino feliz nunca
  fuerza ese error porque el stop que cancela siempre existió de verdad.
- Supuestos 6, 7 y 8 (configuración de cuenta por símbolo): el brief avisa
  de que podrían no ser comprobables si la cuenta de demo no expone esta
  configuración -aquí se sondean, y si el endpoint entero falla, se reporta
  como "no comprobable" en vez de adjudicar el supuesto por un error que no
  distingue entre las dos causas.
- Supuesto 9 también se sondea aquí de forma independiente y ligera (además
  de en el paso 1 del ciclo), porque `get_saldo()` es la pieza que más
  bloquearía el resto del banco si fallase, y conviene poder diagnosticarla
  sola.
"""
from __future__ import annotations

import math
import re

import pytest

from .seguridad import SYMBOL_DEMO

pytestmark = pytest.mark.integracion

_RE_CODE = re.compile(r"code=(\S+)")


def _extraer_code(mensaje: str) -> str | None:
    m = _RE_CODE.search(mensaje)
    return m.group(1) if m else None


async def test_supuesto_1_codigo_de_bitget_al_cancelar_un_stop_inexistente(privado):
    """SUPUESTO 1 (el más arriesgado, Task 6): `BitgetBroker.cancelar_stop`
    trata CUALQUIER `RuntimeError` de `BitgetPrivate.cancelar_stop` como "el
    stop ya no existe" (ejecutado o cancelado antes), sin mirar el `code`.
    Puede estar ocultando un fallo de autenticación o de parámetros.

    Este test sondea `BitgetPrivate` DIRECTAMENTE (sin pasar por la
    idempotencia de `BitgetBroker`) contra un `stop_id` que nunca existió,
    para capturar el `code` real que devuelve Bitget.

    NO SE PUDO EJECUTAR EN ESTA MAQUINA (sin claves de demo) — el código
    real queda sin observar. Quien lo ejecute con claves reales debe:
    1. Correr este test con `-s` y leer la línea "SUPUESTO 1 OBSERVADO".
    2. Si el `code` es estable entre varias ejecuciones, escribirlo en
       `task-12-report.md` y valorar (en otra ronda, no aquí — restricción
       del brief) si `BitgetBroker.cancelar_stop` debería estrechar su
       `except RuntimeError` a ese código concreto en vez de capturar
       cualquier error.
    """
    stop_id_inexistente = "999999999999999999"  # nunca ha sido un orderId real
    with pytest.raises(RuntimeError) as exc_info:
        await privado.cancelar_stop(symbol=SYMBOL_DEMO, stop_id=stop_id_inexistente)

    mensaje = str(exc_info.value)
    codigo = _extraer_code(mensaje)
    assert codigo is not None, (
        f"SUPUESTO 1 CAYO EN LA FORMA DEL ERROR, no solo en el fondo: se "
        f"esperaba un mensaje con 'code=<algo>' (formato fijo que usa "
        f"_pedir() en private.py) y llegó: {mensaje!r}. Bitget puede estar "
        f"devolviendo, para este caso concreto, un sobre de error distinto "
        f"al habitual (`code`/`msg` en el nivel superior del JSON)."
    )
    print(
        f"\nSUPUESTO 1 OBSERVADO: cancelar un stop inexistente en "
        f"{SYMBOL_DEMO!r} devuelve code={codigo!r} (mensaje completo: "
        f"{mensaje!r}). Si este código es estable, es el candidato para "
        f"estrechar el `except RuntimeError` de BitgetBroker.cancelar_stop."
    )


async def test_supuestos_6_7_8_configuracion_de_cuenta_por_simbolo(privado):
    """SUPUESTOS 6, 7 y 8 (Task 11), los tres en el mismo endpoint:

    6. Endpoint `GET /api/v2/mix/account/account` (singular), con `symbol`
       + `marginCoin` + `productType`.
    7. Campo `marginMode`, con valores `"isolated"` / `"crossed"`.
    8. Campos `isolatedLongLever` / `isolatedShortLever` para el
       apalancamiento por lado.

    El brief avisa explícitamente de que estos tres podrían no ser
    comprobables si la cuenta de simulación no expone esta configuración.
    Este test NO asume que el endpoint tenga que responder con éxito: si
    Bitget lo rechaza (404, "not supported", lo que sea), se reporta como
    NO COMPROBABLE en vez de darlo por falso -un rechazo del endpoint
    entero no distingue entre "el endpoint no existe en demo" y "el
    endpoint existe pero el nombre de un campo está mal", así que no se
    puede adjudicar el supuesto desde ese error.

    `get_configuracion_symbol` (private.py) NO falla cerrado en los nombres
    de campo -a diferencia de `get_saldo()`- usa `.get(..., 0)` /
    `.get("marginMode") == "isolated"`, así que si `isolatedLongLever` o
    `isolatedShortLever` llegasen con otro nombre, esta función no lo
    notaría por sí sola: por eso este test comprueba explícitamente que al
    menos uno de los dos apalancamientos observados sea mayor que cero.
    """
    try:
        config = await privado.get_configuracion_symbol(SYMBOL_DEMO)
    except RuntimeError as exc:
        pytest.skip(
            f"SUPUESTOS 6/7/8 NO COMPROBABLES en esta cuenta de demo: "
            f"get_configuracion_symbol({SYMBOL_DEMO!r}) fallo con: {exc}. "
            f"Puede ser que el endpoint no este disponible en demo trading, "
            f"o que el supuesto 6 (la ruta) sea falso -este error, por si "
            f"solo, no permite distinguir entre las dos causas. Revisar "
            f"manualmente contra la documentacion de Bitget si hace falta "
            f"confirmar esto de verdad."
        )

    print(
        f"\nSUPUESTOS 6/7/8 OBSERVADO para {SYMBOL_DEMO!r}: "
        f"margen_aislado={config.margen_aislado!r} "
        f"apalancamiento_long={config.apalancamiento_long!r} "
        f"apalancamiento_short={config.apalancamiento_short!r}"
    )
    assert config.apalancamiento_long > 0 or config.apalancamiento_short > 0, (
        f"SUPUESTO 7 U 8 SOSPECHOSO: el endpoint respondio (supuesto 6 "
        f"parece correcto) pero AMBOS apalancamientos salieron 0. O bien "
        f"'isolatedLongLever'/'isolatedShortLever' son nombres de campo "
        f"incorrectos y get_configuracion_symbol los esta leyendo como "
        f"ausentes (default 0 via .get(..., 0)), o la cuenta de demo de "
        f"verdad no tiene apalancamiento configurado en {SYMBOL_DEMO!r} -"
        f"en ese caso, configúralo una vez en la interfaz de Bitget y "
        f"repite este test."
    )


async def test_supuesto_9_accountequity_y_unrealizedpl_no_faltan(privado):
    """SUPUESTO 9: `accountEquity` y `unrealizedPL` en
    `/api/v2/mix/account/accounts`. `get_saldo()` ya falla cerrado (lanza
    `RuntimeError`) si cualquiera de los dos falta -Task 11-, así que aquí
    basta con comprobar que la llamada no lanza y que el resultado es
    coherente (equity = realizado + pnl_no_realizado, por construcción).

    Sondeo independiente y ligero del que ya hace el paso 1 de
    `test_ciclo_completo.py`, para poder diagnosticar esta pieza sola sin
    depender de que el resto del ciclo llegue a ejecutarse.
    """
    try:
        saldo = await privado.get_saldo()
    except RuntimeError as exc:
        pytest.fail(
            f"SUPUESTO 9 CAYO: get_saldo() lanzo (falla cerrado, como se "
            f"espera si 'accountEquity' o 'unrealizedPL' faltan o vienen "
            f"con otro nombre): {exc}. Revisar el payload real de "
            f"/api/v2/mix/account/accounts y actualizar el informe de la "
            f"Task 12 con la forma observada -NO tocar private.py desde "
            f"este banco."
        )
    print(
        f"\nSUPUESTO 9 OBSERVADO: saldo.equity={saldo.equity!r} "
        f"saldo.pnl_no_realizado={saldo.pnl_no_realizado!r} "
        f"saldo.realizado={saldo.realizado!r}"
    )
    assert math.isclose(saldo.realizado, saldo.equity - saldo.pnl_no_realizado, rel_tol=1e-9)
