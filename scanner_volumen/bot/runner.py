"""El bucle del bot: convierte eventos del scanner en órdenes.

En cada tick recibe las transiciones que el evaluador acaba de producir y una
función que da el último precio observado de cada símbolo. Por cada posición
viva construye una **vela sintética** (`open = high = low = close = precio`) y
se la pasa al motor de reglas junto con las transiciones de ese símbolo.

Que la vela no tenga mechas es deliberado: el bot solo reacciona a precios que
realmente observó, igual que un operador real. El backtest, que sí ve el máximo
y el mínimo de cada minuto, es en ese sentido más optimista, y medir esa
diferencia es parte del objetivo de la Fase 2.

Las salidas se procesan ANTES que las entradas, para que una posición que
cierra en este mismo tick libere su hueco de concurrencia — igual que hace el
backtest.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from scanner_volumen.bot.broker import Broker
from scanner_volumen.bot.model import PosicionAbierta
from scanner_volumen.bot.portfolio import LivePortfolio
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.config import BotConfig
from scanner_volumen.models import Direction
from scanner_volumen.strategy.entries import es_entrada
from scanner_volumen.strategy.model import (
    CandleRow, ExitIntent, Fill, StrategyParams, TransitionRow,
)
from scanner_volumen.strategy.position import PositionRules

log = logging.getLogger(__name__)

PrecioDe = Callable[[str], float | None]


class BotRunner:
    def __init__(
        self, params: StrategyParams, cfg_bot: BotConfig, repo: BotRepo,
        broker: Broker, portfolio: LivePortfolio,
    ) -> None:
        self._params = params
        self._cfg = cfg_bot
        self._repo = repo
        self._broker = broker
        self.portfolio = portfolio
        self.abiertas: dict[str, PosicionAbierta] = {}
        self.transiciones_vistas = 0

    async def on_tick(
        self, transiciones: list[TransitionRow], precio_de: PrecioDe, ahora: int,
    ) -> None:
        self.transiciones_vistas += len(transiciones)
        por_simbolo: dict[str, list[TransitionRow]] = {}
        for t in transiciones:
            por_simbolo.setdefault(t.symbol, []).append(t)

        # (1) gobernar lo que ya está abierto: puede liberar huecos
        for symbol in list(self.abiertas):
            await self._avanzar(self.abiertas[symbol], por_simbolo.get(symbol, ()),
                                precio_de, ahora)

        # (2) evaluar entradas nuevas
        for t in transiciones:
            if not es_entrada(t):
                continue
            precio = precio_de(t.symbol)
            if precio is None or precio <= 0:
                continue  # sin precio observado no se entra; no es un descarte
            if self.portfolio.evaluar_entrada(t, set(self.abiertas), precio) is None:
                await self._abrir(t, precio, ahora)

    # --- entradas ---

    async def _abrir(self, t: TransitionRow, precio: float, ahora: int) -> None:
        margin = self.portfolio.margen()
        notional = margin * self._params.apalancamiento
        orden = await self._broker.abrir(
            symbol=t.symbol, direction=t.direction, notional=notional,
            precio_mercado=precio, ts=ahora,
        )
        posicion_id = self._repo.abrir(
            modo=self._cfg.modo, symbol=t.symbol, direction=t.direction,
            entry_ts=ahora, entry_price=orden.precio, entry_price_senal=t.price,
            margin=margin, notional=notional, size=orden.cantidad,
            fee_entrada=orden.comision,
        )
        # el motor se ancla al precio EJECUTADO: el stop, el break-even y el
        # estancamiento deben medirse desde donde la posición está de verdad
        entrada_real = TransitionRow(
            ts=ahora, symbol=t.symbol, prev_state=t.prev_state,
            new_state=t.new_state, price=orden.precio, direction=t.direction,
            score=t.score,
        )
        self.abiertas[t.symbol] = PosicionAbierta(
            id=posicion_id, symbol=t.symbol, direction=t.direction,
            entry_ts=ahora, entry_price=orden.precio, entry_price_senal=t.price,
            margin=margin, notional=notional, size=orden.cantidad,
            reglas=PositionRules(entrada_real, self._params),
            pnl_acumulado=-orden.comision, fees_acumuladas=orden.comision,
        )
        log.info("bot: abre %s %s a %.6g (senal %.6g), margen %.2f",
                 t.symbol, t.direction.value, orden.precio, t.price, margin)

    # --- posiciones vivas ---

    async def _avanzar(
        self, pos: PosicionAbierta, transiciones, precio_de: PrecioDe, ahora: int,
    ) -> None:
        precio = precio_de(pos.symbol)
        if precio is None or precio <= 0:
            return  # sin precio observado no se evalúa nada este tick
        vela = CandleRow(ts=ahora, open=precio, high=precio, low=precio,
                         close=precio)
        for intent in pos.reglas.on_candle(vela, tuple(transiciones)):
            await self._ejecutar(pos, intent, precio, ahora)
        if pos.reglas.cerrada:
            self._cerrar(pos, ahora)

    async def _ejecutar(
        self, pos: PosicionAbierta, intent: ExitIntent, precio: float, ahora: int,
        tardio: bool = False,
    ) -> None:
        cantidad = pos.size * intent.fraction
        orden = await self._broker.cerrar(
            symbol=pos.symbol, direction=pos.direction, cantidad=cantidad,
            precio_mercado=precio, ts=ahora,
        )
        pos.reglas.on_fill(Fill(ts=intent.ts, price=orden.precio,
                                fraction=intent.fraction, reason=intent.reason))
        signo = 1.0 if pos.direction is Direction.LONG else -1.0
        bruto = signo * (orden.precio - pos.entry_price) * cantidad
        pos.pnl_acumulado += bruto - orden.comision
        pos.fees_acumuladas += orden.comision
        self._repo.registrar_fill(
            pos.id, ts=intent.ts, reason=intent.reason, fraction=intent.fraction,
            precio_referencia=intent.precio_referencia, precio=orden.precio,
            comision=orden.comision, tardio=tardio,
        )

    def _cerrar(self, pos: PosicionAbierta, ahora: int) -> None:
        self._repo.cerrar(pos.id, close_ts=ahora, pnl=pos.pnl_acumulado,
                          fees=pos.fees_acumuladas, max_rank=pos.reglas.max_rank)
        self.portfolio.registrar_cierre(pos.symbol, ahora, pos.pnl_acumulado)
        self.abiertas.pop(pos.symbol, None)
        log.info("bot: cierra %s pnl %.2f (equity %.2f)",
                 pos.symbol, pos.pnl_acumulado, self.portfolio.equity())
