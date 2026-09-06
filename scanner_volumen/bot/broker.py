"""Interfaz de ejecución y su implementación en paper.

El bot no sabe si opera con dinero real: pide al broker que abra o cierre y
recibe una `OrdenEjecutada` con el precio que se obtuvo de verdad. La Fase 3
añadirá un `BitgetBroker` detrás de esta misma interfaz sin que el resto del
bot cambie.

`precio_mercado` es el precio observado en ese instante. El `PaperBroker`
rellena ahí mismo; un broker real lo usará como referencia y devolverá lo que
el exchange le dé. El precio que la REGLA pedía no viaja hasta aquí: lo
registra quien llama, porque el broker no tiene por qué conocer la intención de
la estrategia.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from scanner_volumen.bot.model import OrdenEjecutada
from scanner_volumen.models import Direction
from scanner_volumen.strategy.model import StrategyParams


@dataclass(frozen=True)
class StopVivo:
    """Un stop puesto en el exchange (o, en paper, su simulacro en memoria)."""

    stop_id: str
    symbol: str
    precio_disparo: float
    cantidad: float


class Broker(Protocol):
    async def abrir(
        self, *, symbol: str, direction: Direction, notional: float,
        precio_mercado: float, ts: int, client_oid: str,
    ) -> OrdenEjecutada: ...

    async def cerrar(
        self, *, symbol: str, direction: Direction, cantidad: float,
        precio_mercado: float, ts: int,
    ) -> OrdenEjecutada: ...

    async def colocar_stop(
        self, *, symbol: str, direction: Direction, cantidad: float,
        precio_disparo: float, client_oid: str,
    ) -> str: ...

    async def mover_stop(
        self, *, symbol: str, stop_id: str, precio_disparo: float,
    ) -> str: ...

    async def cancelar_stop(self, *, symbol: str, stop_id: str) -> None: ...


class PaperBroker:
    """Ejecuta contra el precio observado, sin tocar la red.

    No modela deslizamiento propio: el desvío que interesa medir en la Fase 2 es
    el que produce el paso del tiempo -entre que la regla decide y el bot
    actúa, el precio ya se movió-, y eso queda capturado por la diferencia
    entre el precio de referencia de la regla y este precio observado.

    Implementa también el ciclo de vida del stop (`colocar_stop`,
    `mover_stop`, `cancelar_stop`) aunque en paper no lo necesita -aquí nadie
    puede liquidar la posición si el proceso muere-. Lo hace para que el
    camino de código del `BotRunner` sea idéntico en los tres modos (paper,
    lectura y real) y para que el ciclo del stop se pueda probar sin tocar la
    red; el `BitgetBroker` de la Fase 3 implementará el mismo `Protocol`
    contra el exchange de verdad.
    """

    def __init__(self, params: StrategyParams) -> None:
        self._params = params
        self._stops: dict[str, StopVivo] = {}

    async def abrir(
        self, *, symbol: str, direction: Direction, notional: float,
        precio_mercado: float, ts: int, client_oid: str,
    ) -> OrdenEjecutada:
        """`client_oid` identifica la orden ante un exchange real (ver
        `BotRunner._abrir`, que reserva la fila con él antes de llamar aquí).
        Es obligatorio a propósito -no lleva valor por defecto-: un `Broker`
        sin clave de idempotencia es exactamente el agujero que esta tarea
        cierra, y dejarla opcional permitiría a un llamador futuro (la Fase
        3, un test, código de reconciliación) mandar una orden real sin
        ella. El `PaperBroker` no habla con ningún exchange, así que lo
        acepta y lo ignora."""
        if precio_mercado <= 0:
            raise ValueError(
                f"precio_mercado debe ser estrictamente positivo, recibido: {precio_mercado}"
            )
        return OrdenEjecutada(
            ts=ts, precio=precio_mercado, cantidad=notional / precio_mercado,
            comision=self._params.comision_taker * notional,
        )

    async def cerrar(
        self, *, symbol: str, direction: Direction, cantidad: float,
        precio_mercado: float, ts: int,
    ) -> OrdenEjecutada:
        if precio_mercado <= 0:
            raise ValueError(
                f"precio_mercado debe ser estrictamente positivo, recibido: {precio_mercado}"
            )
        return OrdenEjecutada(
            ts=ts, precio=precio_mercado, cantidad=cantidad,
            comision=self._params.comision_taker * cantidad * precio_mercado,
        )

    async def colocar_stop(
        self, *, symbol: str, direction: Direction, cantidad: float,
        precio_disparo: float, client_oid: str,
    ) -> str:
        """`direction` y `client_oid` no se usan en paper -no hay lado que
        distinga el exchange ni orden real que idempotizar-, pero forman
        parte del `Protocol` porque el `BitgetBroker` sí los necesita."""
        if precio_disparo <= 0:
            raise ValueError(
                f"precio_disparo debe ser estrictamente positivo, recibido: {precio_disparo}"
            )
        stop_id = str(uuid.uuid4())
        self._stops[symbol] = StopVivo(
            stop_id=stop_id, symbol=symbol, precio_disparo=precio_disparo,
            cantidad=cantidad,
        )
        return stop_id

    async def mover_stop(
        self, *, symbol: str, stop_id: str, precio_disparo: float,
    ) -> str:
        """Sustituye el stop vivo de `symbol` por uno nuevo al precio dado.

        En un exchange real mover un stop es cancelar el viejo y colocar
        otro -no hay una orden "editar"-, así que el `stop_id` cambia. Aquí
        se refleja lo mismo: se conserva un único stop vivo por símbolo."""
        if precio_disparo <= 0:
            raise ValueError(
                f"precio_disparo debe ser estrictamente positivo, recibido: {precio_disparo}"
            )
        anterior = self._stops.get(symbol)
        cantidad = anterior.cantidad if anterior is not None else 0.0
        nuevo_id = str(uuid.uuid4())
        self._stops[symbol] = StopVivo(
            stop_id=nuevo_id, symbol=symbol, precio_disparo=precio_disparo,
            cantidad=cantidad,
        )
        return nuevo_id

    async def cancelar_stop(self, *, symbol: str, stop_id: str) -> None:
        """No lanza si el stop ya no existe: en real puede haberse ejecutado
        ya -que es justo lo que queremos que pase-, y cancelarlo tiene que
        ser una operación idempotente para no reventar el cierre normal de
        una posición cuyo stop saltó."""
        actual = self._stops.get(symbol)
        if actual is not None and actual.stop_id == stop_id:
            del self._stops[symbol]

    def stops_vivos(self) -> dict[str, StopVivo]:
        """Accesor de solo lectura para los tests: no lo consume el runner."""
        return dict(self._stops)
