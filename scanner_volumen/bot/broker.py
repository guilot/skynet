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

from typing import Protocol

from scanner_volumen.bot.model import OrdenEjecutada
from scanner_volumen.models import Direction
from scanner_volumen.strategy.model import StrategyParams


class Broker(Protocol):
    async def abrir(
        self, *, symbol: str, direction: Direction, notional: float,
        precio_mercado: float, ts: int, client_oid: str | None = None,
    ) -> OrdenEjecutada: ...

    async def cerrar(
        self, *, symbol: str, direction: Direction, cantidad: float,
        precio_mercado: float, ts: int,
    ) -> OrdenEjecutada: ...


class PaperBroker:
    """Ejecuta contra el precio observado, sin tocar la red.

    No modela deslizamiento propio: el desvío que interesa medir en la Fase 2 es
    el que produce el paso del tiempo -entre que la regla decide y el bot
    actúa, el precio ya se movió-, y eso queda capturado por la diferencia
    entre el precio de referencia de la regla y este precio observado.
    """

    def __init__(self, params: StrategyParams) -> None:
        self._params = params

    async def abrir(
        self, *, symbol: str, direction: Direction, notional: float,
        precio_mercado: float, ts: int, client_oid: str | None = None,
    ) -> OrdenEjecutada:
        """`client_oid` identifica la orden ante un exchange real (ver
        `BotRunner._abrir`, que reserva la fila con él antes de llamar aquí);
        el paper no habla con ningún exchange, así que lo acepta y lo ignora."""
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
