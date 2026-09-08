"""Banco de pruebas de integración contra la simulación (demo trading) de
Bitget — Task 12. Ejercita el ÚNICO código del proyecto que manda órdenes
de verdad (`BitgetPrivate`, `BitgetBroker`) contra un exchange real, algo
que hasta esta tarea solo se había probado contra dobles de test.

## Qué claves hacen falta

Tres variables de entorno, **exclusivas de demo** (ver `seguridad.py` —
no hay ningún camino de código en este banco que lea ninguna otra):

    SCANNER_BITGET_DEMO_KEY
    SCANNER_BITGET_DEMO_SECRET
    SCANNER_BITGET_DEMO_PASSPHRASE

Si falta cualquiera, `conftest.py` hace `pytest.skip` con un mensaje que lo
explica — este banco NUNCA falla en una máquina sin claves, se salta.

## Cómo se obtienen en Bitget

1. Entra en tu cuenta de Bitget (web o app) y activa el **"Demo Trading"**
   (a fecha de escribir esto vive como una opción/interruptor en la propia
   interfaz de trading de futuros, o en el menú de tu perfil — Bitget
   reorganiza su interfaz de vez en cuando, así que si no la encuentras
   donde se describe aquí, busca "demo trading" en su ayuda/soporte). Te da
   una subcuenta con fondos **virtuales** en USDT, separada de tu cuenta
   real.
2. Dentro de esa vista de demo trading, ve a gestión de claves API
   ("API Management" / "API Keys") — Bitget expone claves DISTINTAS para
   demo trading que para la cuenta real, aunque el flujo de creación se
   parezca. Crea una clave con permisos de **lectura y trading de
   futuros** (no hace falta retiro de fondos: este banco nunca retira).
3. Copia clave, secreto y passphrase en las tres variables de arriba, en
   el entorno desde el que vayas a ejecutar pytest (nunca en el repo,
   nunca en un commit).

## Qué es esta cuenta

Es una simulación: fondos virtuales, mismos endpoints y misma autenticación
que producción, pero seleccionada explícitamente por `productType =
"SUSDT-FUTURES"` y símbolos con prefijo `S` (`SBTCSUSDT`). **No mueve
dinero real** — y el propio banco se niega a operar (`seguridad.py`,
`_verificar_entorno_de_simulacion`) si alguna vez detecta un `productType`
o un símbolo que no sean inequívocamente de esta simulación.

## Cómo se ejecuta

    .venv/bin/pytest tests/integracion_bitget -m integracion -v

Añade `-s` si además quieres ver en la terminal las observaciones que este
banco imprime sobre los supuestos de la API que confirma (con `-v` solo, un
test que PASA no muestra su salida estándar — sí la muestra automáticamente
si falla).

No se ejecuta en cada cambio: es un banco manual, para cuando hay algo que
verificar contra el exchange de verdad (aunque sea de mentira).
"""
from __future__ import annotations

import logging
import math
import time
import uuid

import httpx
import pytest

from scanner_volumen.bitget.rest import BitgetRest
from scanner_volumen.models import Direction

from .diagnostico import bajo_sospecha
from .seguridad import PRODUCT_TYPE_DEMO, SYMBOL_DEMO

log = logging.getLogger(__name__)

pytestmark = pytest.mark.integracion

RATE_LIMIT_PUBLICO = 10.0
# Nocional de referencia en USDT. Es dinero virtual de la cuenta de demo,
# así que el tamaño exacto no importa; solo tiene que ser lo bastante
# grande para no chocar con el tamaño mínimo de lote de Bitget en el
# símbolo de demo (desconocido desde aquí, sin acceso a los metadatos del
# contrato — ver el riesgo ya anotado en task-6-report.md sobre
# `_formato_decimal`/redondeo de contrato).
NOTIONAL_USDT = 200.0


async def _precio_mercado_actual() -> float:
    """Precio de referencia PÚBLICO (sin autenticar) para dimensionar la
    orden. No participa en el resultado del ciclo -que sale siempre del
    fill real, nunca de esta referencia-, así que un desvío aquí no
    invalida el test; solo evitaría pedir una cantidad absurda si el precio
    real estuviera muy lejos de cualquier valor fijo en el código."""
    async with httpx.AsyncClient(timeout=10.0) as cliente_http:
        rest = BitgetRest(PRODUCT_TYPE_DEMO, RATE_LIMIT_PUBLICO, cliente_http)
        tickers = await rest.get_tickers()
    for ticker in tickers:
        if ticker.symbol == SYMBOL_DEMO:
            return ticker.last
    raise RuntimeError(
        f"{SYMBOL_DEMO!r} no aparece en get_tickers() para productType="
        f"{PRODUCT_TYPE_DEMO!r} -revisar si el simbolo de demo sigue "
        f"existiendo con ese nombre en Bitget."
    )


async def test_ciclo_completo_abrir_stop_mover_cerrar_cancelar(broker, privado):
    """El ciclo completo del Step 2 del brief: saldo -> abrir -> aparece en
    posiciones -> stop -> mover stop -> cerrar -> cancelar stop (idempotente)
    -> fill razonable. Cada paso de escritura va envuelto en `bajo_sospecha`
    para que, si Bitget lo rechaza, el mensaje diga qué supuesto de la lista
    puede estar detrás -no un `RuntimeError` desnudo con solo el `code`.

    LIMPIEZA: el `finally` relee el estado REAL del exchange (no un flag
    local) porque una excepción puede ocurrir DESPUÉS de que Bitget ya
    colocara la orden -por ejemplo, dentro de `broker.abrir`, entre
    `colocar_orden` y `get_fill`-, y un flag local no se enteraría de que la
    posición sí llegó a abrirse.
    """
    stop_ids_vistos: list[str] = []
    precio_mercado: float | None = None

    try:
        # 1. Saldo positivo: valida la firma HMAC, la pieza que más veces
        # ha mordido en esta fase (progress.md, Task 2).
        with bajo_sospecha(
            "1: leer saldo (valida la firma HMAC)",
            "supuesto 9 (accountEquity/unrealizedPL en /account/accounts)",
        ):
            saldo = await privado.get_saldo()
        assert saldo.realizado > 0, (
            f"la firma funciono (Bitget respondio code=00000) pero el saldo "
            f"realizado observado es {saldo.realizado!r}, no positivo -"
            f"revisa si la cuenta de demo tiene fondos virtuales asignados "
            f"(se piden solos al activar demo trading, pero pueden agotarse)."
        )

        precio_mercado = await _precio_mercado_actual()
        ts_apertura = int(time.time() * 1000)
        client_oid = f"banco-t12-{uuid.uuid4().hex}"

        # 2. Abrir una posición pequeña (para la cuenta) a mercado.
        with bajo_sospecha(
            "2: abrir posicion a mercado (place-order + fills)",
            "supuesto 4 (forma de la respuesta de /order/fills)",
        ):
            orden = await broker.abrir(
                symbol=SYMBOL_DEMO,
                direction=Direction.LONG,
                notional=NOTIONAL_USDT,
                precio_mercado=precio_mercado,
                ts=ts_apertura,
                client_oid=client_oid,
            )
        assert orden.cantidad > 0 and orden.precio > 0, (
            f"SUPUESTO 4 POSIBLEMENTE CAIDO: la orden se coloco sin "
            f"excepcion, pero el fill agregado salio cantidad="
            f"{orden.cantidad!r} precio={orden.precio!r} -alguno de los dos "
            f"deberia ser positivo si Bitget devolvio fills reales en "
            f"data.fillList. Revisar la forma real de la respuesta de "
            f"/api/v2/mix/order/fills."
        )
        desvio_apertura = abs(orden.precio - precio_mercado) / precio_mercado
        assert desvio_apertura < 0.05, (
            f"el precio del fill de apertura ({orden.precio}) se desvia un "
            f"{desvio_apertura:.1%} del precio de mercado de referencia "
            f"({precio_mercado}) -mas de lo razonable para una orden a "
            f"mercado; podria ser sintoma de que get_fill esta leyendo el "
            f"campo equivocado (supuesto 4), no solo slippage."
        )

        # 3. Aparece en las posiciones del exchange.
        posiciones = await privado.get_posiciones()
        pos = next((p for p in posiciones if p.symbol == SYMBOL_DEMO), None)
        assert pos is not None, (
            f"la apertura no lanzo y devolvio un fill, pero {SYMBOL_DEMO!r} "
            f"no aparece en get_posiciones() -revisar si el 'symbol' que "
            f"devuelve Bitget en /position/all-position coincide EXACTAMENTE "
            f"con el que se pidio."
        )
        assert pos.lado == "long", (
            f"se abrio un LONG pero get_posiciones() devuelve lado="
            f"{pos.lado!r} (se esperaba 'long', derivado en minusculas del "
            f"'holdSide' que reporta Bitget)."
        )
        assert math.isclose(pos.tamano, orden.cantidad, rel_tol=0.05), (
            f"el tamano que reporta Bitget en la posicion ({pos.tamano}) "
            f"difiere mas de un 5% de la cantidad del fill de apertura "
            f"({orden.cantidad}) -revisar redondeo de lote, o si 'total' en "
            f"all-position no es el campo que se esperaba."
        )

        # 4. Colocar un stop reduce-only y verificarlo (se coloca; la
        # verificacion de que existe es indirecta: si Bitget lo rechazase,
        # este paso lanzaria).
        precio_stop = orden.precio * 0.8  # 20% por debajo: lejos del ruido normal
        with bajo_sospecha(
            "4: colocar stop (place-tpsl-order)",
            "supuesto 2 (holdSide, no side) y supuesto 3 (planType='loss_plan')",
        ):
            stop_id = await broker.colocar_stop(
                symbol=SYMBOL_DEMO,
                direction=Direction.LONG,
                cantidad=orden.cantidad,
                precio_disparo=precio_stop,
                client_oid=f"banco-t12-stop-{uuid.uuid4().hex}",
            )
        stop_ids_vistos.append(stop_id)
        assert stop_id, (
            "colocar_stop no lanzo pero devolvio un stop_id vacio -Bitget "
            "puede no estar devolviendo 'orderId' en data para este endpoint."
        )

        # 5. Moverlo y verificar el precio nuevo. LIMITACION CONOCIDA:
        # BitgetPrivate no tiene ningun metodo de LECTURA de plan orders
        # vivos (fuera del alcance de esta tarea anadir uno -no se toca
        # private.py-), asi que "verificar el precio nuevo" se limita aqui
        # a que Bitget aceptase la modificacion sin lanzar. Confirmar el
        # precio de verdad en el lado de Bitget exigiria una consulta que
        # hoy no existe en el cliente.
        precio_stop_2 = orden.precio * 0.75
        with bajo_sospecha(
            "5: mover stop (modify-tpsl-order)",
            "supuesto 5 (si modify-tpsl-order conserva el orderId)",
        ):
            stop_id_tras_mover = await broker.mover_stop(
                symbol=SYMBOL_DEMO, stop_id=stop_id, precio_disparo=precio_stop_2,
            )
        stop_ids_vistos.append(stop_id_tras_mover)
        assert stop_id_tras_mover, (
            "modify-tpsl-order no lanzo pero no devolvio ningun orderId "
            "(ni el nuevo ni, como fallback, el original)."
        )
        if stop_id_tras_mover != stop_id:
            print(
                f"\nSUPUESTO 5 OBSERVADO: modify-tpsl-order devolvio un "
                f"orderId NUEVO ({stop_id_tras_mover!r}) distinto del "
                f"original ({stop_id!r}) -Bitget SI cambia el orderId al "
                f"modificar; mover_stop() ya lo maneja bien, pero anotar "
                f"esto en el informe."
            )
        else:
            print(
                f"\nSUPUESTO 5 OBSERVADO: modify-tpsl-order CONSERVA el "
                f"mismo orderId ({stop_id!r}) tras modificar el precio."
            )

        # 6. Cerrar la posición a mercado.
        with bajo_sospecha(
            "6: cerrar posicion a mercado (place-order reduce-only + fills)",
            "supuesto 4 (forma de la respuesta de /order/fills)",
        ):
            cierre = await broker.cerrar(
                symbol=SYMBOL_DEMO,
                direction=Direction.LONG,
                cantidad=orden.cantidad,
                precio_mercado=precio_mercado,
                ts=int(time.time() * 1000),
            )

        # 7. Cancelar el stop, que ya no debería existir -no debe lanzar.
        # cancelar_stop es idempotente por diseño (BitgetBroker), así que
        # esto en sí mismo no prueba nada sobre el supuesto 1; el sondeo
        # dedicado vive en test_supuestos_api.py.
        await broker.cancelar_stop(symbol=SYMBOL_DEMO, stop_id=stop_id_tras_mover)

        # 8. El fill del cierre es un número razonable.
        assert cierre.cantidad > 0 and math.isfinite(cierre.precio) and cierre.precio > 0, (
            f"el cierre no lanzo pero el fill agregado salio cantidad="
            f"{cierre.cantidad!r} precio={cierre.precio!r} -mismo supuesto 4 "
            f"que en la apertura."
        )
        desvio_cierre = abs(cierre.precio - precio_mercado) / precio_mercado
        assert desvio_cierre < 0.05, (
            f"el precio del fill de cierre ({cierre.precio}) se desvia un "
            f"{desvio_cierre:.1%} del precio de mercado de referencia "
            f"({precio_mercado})."
        )

    finally:
        # Limpieza incondicional (brief, punto 3): "limpia lo que abras,
        # pase lo que pase". Cancela todos los stop_id que se hayan visto
        # -cancelar_stop es idempotente, así que reintentar sobre uno ya
        # cancelado o superado por un `mover_stop` posterior es inofensivo-
        # y cierra cualquier posición que de verdad quede abierta en
        # SYMBOL_DEMO, leyendo el estado REAL del exchange, no un flag local.
        for sid in dict.fromkeys(stop_ids_vistos):  # dedup conservando orden
            try:
                await broker.cancelar_stop(symbol=SYMBOL_DEMO, stop_id=sid)
            except Exception:
                log.exception(
                    "LIMPIEZA: cancelar_stop(%s) en %s lanzo algo "
                    "inesperado (no el RuntimeError que se traga la "
                    "idempotencia) -revisar la cuenta de simulacion A MANO.",
                    sid, SYMBOL_DEMO,
                )

        if precio_mercado is None:
            try:
                precio_mercado = await _precio_mercado_actual()
            except Exception:
                log.exception(
                    "LIMPIEZA: no se pudo obtener un precio de mercado de "
                    "referencia para poder cerrar una posible posicion "
                    "residual -si el test fallo antes del paso 2, revisar "
                    "la cuenta de simulacion A MANO en la interfaz de Bitget."
                )

        try:
            posiciones_finales = await privado.get_posiciones()
        except Exception:
            log.exception(
                "LIMPIEZA: no se pudo leer get_posiciones() para comprobar "
                "si queda algo abierto -REVISAR LA CUENTA DE SIMULACION A "
                "MANO antes de volver a ejecutar este banco."
            )
            posiciones_finales = []

        for pos in posiciones_finales:
            if pos.symbol != SYMBOL_DEMO or pos.tamano <= 0:
                continue
            if precio_mercado is None:
                log.error(
                    "LIMPIEZA INCOMPLETA: queda una posicion abierta de %s "
                    "en %s y no hay precio de mercado para cerrarla desde "
                    "aqui -CERRARLA A MANO en la interfaz de Bitget.",
                    pos.tamano, SYMBOL_DEMO,
                )
                continue
            direccion_residual = Direction.LONG if pos.lado == "long" else Direction.SHORT
            try:
                await broker.cerrar(
                    symbol=SYMBOL_DEMO,
                    direction=direccion_residual,
                    cantidad=pos.tamano,
                    precio_mercado=precio_mercado,
                    ts=int(time.time() * 1000),
                )
                log.warning(
                    "LIMPIEZA: se cerro una posicion residual de %s en %s "
                    "que quedo abierta a mitad del test -investigar en que "
                    "paso fallo.", pos.tamano, SYMBOL_DEMO,
                )
            except Exception:
                log.exception(
                    "LIMPIEZA: no se pudo cerrar la posicion residual de %s "
                    "en %s -REVISAR LA CUENTA DE SIMULACION A MANO.",
                    pos.tamano, SYMBOL_DEMO,
                )
