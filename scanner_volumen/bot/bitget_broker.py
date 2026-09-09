"""`Broker` real: manda órdenes de verdad a Bitget.

Implementa el mismo `Protocol` que `PaperBroker` (ver `broker.py`) traduciendo
entre el vocabulario del bot (`Direction.LONG/SHORT`, fracciones de capital,
cantidades en unidades del activo) y el de Bitget (lados "buy"/"sell",
`reduceOnly`, tamaños en unidades del contrato). Toda la comunicación con la
red vive en `BitgetPrivate` (`scanner_volumen/bitget/private.py`); esta clase
no construye peticiones HTTP ni firma nada, solo decide QUÉ pedir.

Dos propiedades no negociables, heredadas del spec (§6, §7.2):

1. Todas las órdenes de cierre y todos los stops van reduce-only. Es lo que
   permite que una orden duplicada -por un reintento, por el sondeo y el
   stop local disparando casi a la vez- nunca abra una posición en sentido
   contrario.
2. El precio y la cantidad de la `OrdenEjecutada` salen siempre del fill
   real, consultado después de colocar la orden -nunca del `precio_mercado`
   que llega como referencia-. Es lo que permite medir el desvío de
   ejecución real en modo `real`.
"""
from __future__ import annotations

import logging
import uuid

from scanner_volumen.bitget.private import BitgetPrivate
from scanner_volumen.bot.model import OrdenEjecutada
from scanner_volumen.models import Direction
from scanner_volumen.strategy.model import StrategyParams

log = logging.getLogger(__name__)


def _lado_apertura(direction: Direction) -> str:
    """Lado de la orden que ABRE una posición en la dirección dada."""
    return "buy" if direction == Direction.LONG else "sell"


def _lado_cierre(direction: Direction) -> str:
    """Lado de la orden que CIERRA una posición en la dirección dada (el
    contrario al de apertura): vender para cerrar un LONG, comprar para
    cerrar un SHORT. Se usa tanto para `cerrar` como para `colocar_stop`,
    porque un stop es, en el fondo, una orden de cierre condicionada a un
    precio."""
    return "sell" if direction == Direction.LONG else "buy"


class BitgetBroker:
    """`Broker` que opera contra la cuenta real (o de simulación) de Bitget.

    `params` solo se usa por simetría con `PaperBroker` -de momento ningún
    método de esta clase lo necesita, porque la comisión ya no se estima
    (`comision_taker * notional`): sale del fill real, que Bitget reporta ya
    con la comisión cobrada. Se conserva el parámetro para que la factoría
    de la Task 13 pueda construir cualquiera de los dos brokers con la misma
    firma.
    """

    def __init__(self, privado: BitgetPrivate, params: StrategyParams) -> None:
        self._privado = privado
        self._params = params

    async def abrir(
        self, *, symbol: str, direction: Direction, notional: float,
        precio_mercado: float, ts: int, client_oid: str,
    ) -> OrdenEjecutada:
        """Manda una orden a mercado NO reduce-only (abre posición nueva).

        `precio_mercado` solo sirve para traducir `notional` (en USDT) a una
        cantidad de contrato de referencia con la que pedir la orden; el
        precio y la cantidad de la `OrdenEjecutada` devuelta salen del fill
        real, consultado después con `get_fill`.
        """
        if precio_mercado <= 0:
            raise ValueError(
                f"precio_mercado debe ser estrictamente positivo, recibido: {precio_mercado}"
            )
        cantidad_referencia = notional / precio_mercado
        order_id = await self._privado.colocar_orden(
            symbol=symbol,
            lado=_lado_apertura(direction),
            cantidad=cantidad_referencia,
            reduce_only=False,
            client_oid=client_oid,
        )
        fill = await self._privado.get_fill(symbol, order_id)
        return OrdenEjecutada(
            ts=ts, precio=fill.precio, cantidad=fill.cantidad, comision=fill.comision,
        )

    async def cerrar(
        self, *, symbol: str, direction: Direction, cantidad: float,
        precio_mercado: float, ts: int,
    ) -> OrdenEjecutada:
        """Manda una orden a mercado reduce-only en el lado contrario a
        `direction`. El `Protocol` no recibe un `client_oid` para cerrar (a
        diferencia de `abrir` y `colocar_stop`) porque el cierre no participa
        en el esquema de idempotencia de apertura del spec §5.2 -es siempre
        reduce-only, así que reintentarlo nunca puede abrir posición-; se
        genera uno interno solo para que Bitget pueda deduplicar la petición
        a su nivel.
        """
        if precio_mercado <= 0:
            raise ValueError(
                f"precio_mercado debe ser estrictamente positivo, recibido: {precio_mercado}"
            )
        client_oid = f"bot-close-{uuid.uuid4().hex}"
        order_id = await self._privado.colocar_orden(
            symbol=symbol,
            lado=_lado_cierre(direction),
            cantidad=cantidad,
            reduce_only=True,
            client_oid=client_oid,
        )
        fill = await self._privado.get_fill(symbol, order_id)
        return OrdenEjecutada(
            ts=ts, precio=fill.precio, cantidad=fill.cantidad, comision=fill.comision,
        )

    async def colocar_stop(
        self, *, symbol: str, direction: Direction, cantidad: float,
        precio_disparo: float, client_oid: str,
    ) -> str:
        """Coloca un stop reduce-only en el lado contrario a `direction`."""
        if precio_disparo <= 0:
            raise ValueError(
                f"precio_disparo debe ser estrictamente positivo, recibido: {precio_disparo}"
            )
        return await self._privado.colocar_stop(
            symbol=symbol,
            lado=_lado_cierre(direction),
            cantidad=cantidad,
            precio_disparo=precio_disparo,
            client_oid=client_oid,
        )

    async def mover_stop(
        self, *, symbol: str, stop_id: str, precio_disparo: float,
        cantidad: float,
    ) -> str:
        """Modifica el precio de un stop vivo. Si Bitget rechaza la
        modificación porque `stop_id` no corresponde a un plan order vivo
        (`BitgetPrivate.mover_stop` deja pasar el `RuntimeError` de
        `_pedir` sin interpretarlo), se traduce a `ValueError` -misma
        semántica que `PaperBroker.mover_stop`: un `stop_id` que no
        coincide con nada vivo es un error del llamador, no una situación
        normal a tragarse-.
        """
        if precio_disparo <= 0:
            raise ValueError(
                f"precio_disparo debe ser estrictamente positivo, recibido: {precio_disparo}"
            )
        try:
            return await self._privado.mover_stop(
                symbol=symbol, stop_id=stop_id, precio_disparo=precio_disparo,
                cantidad=cantidad,
            )
        except RuntimeError as exc:
            raise ValueError(
                f"no hay stop vivo para {symbol!r} con stop_id={stop_id!r}: {exc}"
            ) from exc

    async def cancelar_stop(self, *, symbol: str, stop_id: str) -> None:
        """Cancela un stop. Idempotente a propósito: si Bitget rechaza la
        cancelación porque el plan order ya no existe -normalmente porque ya
        se ejecutó, que es justo el caso que interesa no tratar como error-,
        se ignora en vez de propagar.

        SUPUESTO SIN VERIFICAR (documentado también en el informe de la
        tarea): se trata CUALQUIER `RuntimeError` de
        `BitgetPrivate.cancelar_stop` como "el stop ya no existe", sin
        distinguir por código de error de Bitget. Es la lectura conservadora
        posible del lado de "no revienta el cierre normal de una posición
        cuyo stop saltó solo"; el riesgo es que oculte un error genuino
        (por ejemplo, de autenticación) detrás de un `code` que no es
        realmente "ya no existe" -de ahí el `log.warning`: si algún día
        resulta ser lo segundo, aquí queda la traza para encontrarlo-.
        Discriminar por el código de error real de Bitget es exactamente lo
        que resolvería esto, y queda pendiente de la tarea que verifica
        contra la cuenta de simulación.
        """
        try:
            await self._privado.cancelar_stop(symbol=symbol, stop_id=stop_id)
        except RuntimeError as exc:
            log.warning(
                "bot: cancelar_stop de %s (stop_id=%s): Bitget rechazo la "
                "cancelacion; se ASUME (no se ha confirmado) que el stop ya no "
                "existe -ejecutado o cancelado antes- y no se propaga. Si el "
                "motivo real fuese otro (autenticacion, parametros...) quedaria "
                "sin mas rastro que este aviso. Error: %s",
                symbol, stop_id, exc,
            )
