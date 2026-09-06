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

Cada posición se gobierna aislada de las demás: si el broker falla al cerrar
(un timeout de red, un precio inválido), la excepción no debe tumbar el tick
entero ni dejar esa posición envenenada. `PositionRules.on_candle` exige que
toda `ExitIntent` se confirme con `on_fill` antes del siguiente evento, y si
el fallo ocurre entre medias esa confirmación nunca llega; sin aislamiento, el
siguiente tick repetiría el `ValueError` de "intenciones sin confirmar" para
siempre. El runner atrapa el fallo, marca la posición como `degradada` y deja
de tocar su motor — pero la conserva en `abiertas`, ocupando su hueco, porque
sigue realmente abierta en la base de datos.
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
    CandleRow, ExitIntent, ExitReason, Fill, StrategyParams, TransitionRow,
)
from scanner_volumen.strategy.position import PositionRules, agrupar_por_vela

log = logging.getLogger(__name__)

PrecioDe = Callable[[str], float | None]

# Máximo número de intentos de cierre para manejar fills parciales.
# Con 3 intentos se cubre el 87.5% de los casos de dos fills parciales
# (0.5 x 0.5 x 0.5 = 12.5% de probabilidad de quedarse corto incluso así).
# Si el broker falla a media ejecución, 3 intentos es un buen balance entre
# insistencia y evitar un bucle infinito.
MAX_INTENTOS_CIERRE = 3

# Tolerancia relativa para considerar que una cantidad está completa.
# Con números en coma flotante, 4.0 - 0.5 - 0.5 - 3.0 no da exactamente
# cero, así que necesitamos comparar con tolerancia. 1e-9 es lo bastante
# pequeño para cantidades típicas (1-100000 unidades) sin ser tan
# estricto que rechace redondeos reales.
TOLERANCIA_CANTIDAD = 1e-9


def _precio_regla_de(
    pos: PosicionAbierta, intent: ExitIntent, stop_vigente: float,
) -> float | None:
    """El nivel que la regla prometía para esta salida -no el precio con el
    que el bot rellena su propia vela sintética, que siempre coincidiría con
    el fill y daría desvío cero por construcción, mida lo que mida.

    `stop_vigente` es el nivel de `pos.reglas.stop_price` capturado por el
    llamador ANTES de la llamada a `on_candle` que produjo `intent`: para
    cuando este código corre, `on_candle` ya pudo haber movido el stop a
    break-even, y lo que hay que medir es contra qué nivel se disparó la
    salida, no contra el nivel que dejó a su paso.
    """
    if intent.reason is ExitReason.STOP:
        return stop_vigente
    if intent.reason is ExitReason.STALE_BE:
        # la regla promete no salir por debajo de break-even
        return pos.entry_price
    if intent.reason in (ExitReason.SCALE_HOT, ExitReason.SCALE_SIGNAL):
        # el precio de la transición que disparó el tramo: un nivel con
        # significado, a diferencia del precio de relleno de la vela.
        return intent.precio_referencia
    # EXTREME (cierre a mercado al vencer el temporizador) y END_OF_DATA: no
    # hay nivel prometido contra el que medir, y aquí el cero SÍ es correcto.
    return None


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
        self.cierres_tardios = 0

    async def on_tick(
        self, transiciones: list[TransitionRow], precio_de: PrecioDe, ahora: int,
    ) -> None:
        self.transiciones_vistas += len(transiciones)
        self._repo.incrementar_contador(self._cfg.modo, "transiciones", len(transiciones))
        por_simbolo: dict[str, list[TransitionRow]] = {}
        for t in transiciones:
            por_simbolo.setdefault(t.symbol, []).append(t)

        # (1) gobernar lo que ya está abierto: puede liberar huecos
        for symbol in list(self.abiertas):
            pos = self.abiertas[symbol]
            try:
                await self._avanzar(pos, por_simbolo.get(symbol, ()), precio_de, ahora)
            except Exception:
                # aislar el fallo a esta posición: no debe tumbar el tick ni
                # dejarla envenenada (con una intención sin confirmar que
                # haría reventar `on_candle` en todos los ticks siguientes).
                # Se queda en `abiertas` ocupando su hueco -sigue realmente
                # abierta en la BD- y la recuperará la reconstrucción al
                # reiniciar (Task 7).
                log.exception("bot: fallo al gobernar %s; se marca degradada", symbol)
                pos.degradada = True
                self._repo.marcar_degradada(pos.id)

        # (2) evaluar entradas nuevas
        for t in transiciones:
            if not es_entrada(t):
                continue
            if t.price is None or t.price <= 0:
                # sin precio de señal (símbolo sin vela en curso) no hay con
                # qué abrir: `repo.abrir` exige `entry_price_senal NOT NULL`.
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
        self._repo.fijar_maximo(
            self._cfg.modo, "max_concurrentes", len(self.abiertas)
        )
        log.info("bot: abre %s %s a %.6g (senal %.6g), margen %.2f",
                 t.symbol, t.direction.value, orden.precio, t.price, margin)

    # --- reconstrucción tras un reinicio ---

    async def reconstruir(
        self, transiciones_de, velas_de, precio_de: PrecioDe, ahora: int,
    ) -> None:
        """Recupera las posiciones que quedaron abiertas en un reinicio.

        Por cada una, replica su historial (transiciones y velas de 1 minuto
        desde la entrada) a través de un motor nuevo, confirmando cada salida
        que YA se ejecutó en vivo con el precio real que se obtuvo entonces.
        Así el motor recupera su estado exacto y la posición sigue como si nada.

        `transiciones_de(symbol, desde_ms)` y `velas_de(symbol, desde_ms)` los
        inyecta el llamador; se pasan como funciones para que el bot no dependa
        de los repositorios concretos ni, sobre todo, del backtest.
        """
        for fila in self._repo.abiertas(self._cfg.modo):
            try:
                await self._reconstruir_una(fila, transiciones_de, velas_de,
                                            precio_de, ahora)
            except Exception:  # noqa: BLE001 - una posición rota no impide arrancar
                log.exception("bot: no se pudo reconstruir %s", fila["symbol"])

    async def _reconstruir_una(
        self, fila, transiciones_de, velas_de, precio_de: PrecioDe, ahora: int,
    ) -> None:
        symbol = fila["symbol"]
        entry_ts = fila["entry_ts"]
        historial = list(transiciones_de(symbol, entry_ts))
        entrada = next((t for t in historial if t.ts == entry_ts), None)
        if entrada is None:
            log.warning("bot: %s abierta sin transición de entrada en la BD; "
                        "se deja fuera del bot (revisar a mano)", symbol)
            return

        direccion = Direction(fila["direction"])
        # el motor se ancla al precio EJECUTADO, igual que al abrir
        entrada_real = TransitionRow(
            ts=entry_ts, symbol=symbol, prev_state=entrada.prev_state,
            new_state=entrada.new_state, price=fila["entry_price"],
            direction=direccion, score=entrada.score,
        )
        pos = PosicionAbierta(
            id=fila["id"], symbol=symbol, direction=direccion, entry_ts=entry_ts,
            entry_price=fila["entry_price"],
            entry_price_senal=fila["entry_price_senal"], margin=fila["margin"],
            notional=fila["notional"], size=fila["size"],
            reglas=PositionRules(entrada_real, self._params),
            pnl_acumulado=-fila["fee_entrada"], fees_acumuladas=fila["fee_entrada"],
        )

        registrados = {f["reason"]: f for f in self._repo.fills_de(fila["id"])}
        posteriores = [t for t in historial if t.ts > entry_ts]
        velas = list(velas_de(symbol, entry_ts))
        por_vela = agrupar_por_vela(posteriores, velas)
        precio_ahora = precio_de(symbol)

        for vela in velas:
            if pos.reglas.cerrada:
                break
            stop_vigente = pos.reglas.stop_price
            for intent in pos.reglas.on_candle(vela, tuple(por_vela.get(vela.ts, ()))):
                ya = registrados.pop(intent.reason.value, None)
                if ya is not None:
                    self._confirmar_registrado(pos, intent, ya)
                elif precio_ahora is not None and precio_ahora > 0:
                    # la réplica ve las mechas del minuto; el bot en vivo solo
                    # veía los precios que observaba. Esta salida debió ocurrir
                    # y no ocurrió: se ejecuta ahora, tarde, y se contabiliza.
                    await self._ejecutar(pos, intent, precio_ahora, ahora,
                                         stop_vigente, tardio=True)
                    self.cierres_tardios += 1
                    log.warning("bot: cierre tardío de %s por %s tras reinicio",
                                symbol, intent.reason.value)
                else:
                    # no hay precio actual para resolver una salida divergente
                    # que la réplica propone y en vivo nunca se confirmó: el
                    # motor se queda con una intención pendiente sin resolver,
                    # igual que si el broker hubiera fallado a media ejecución
                    # en caliente. Se marca degradada -no se vuelve a tocar su
                    # motor- pero se conserva en `abiertas` porque sigue
                    # realmente abierta en la BD; el próximo reinicio volverá
                    # a intentar reconstruirla desde cero.
                    pos.degradada = True
                    self.abiertas[symbol] = pos
                    self._repo.marcar_degradada(pos.id)
                    log.warning(
                        "bot: %s reconstruida como degradada: sin precio para "
                        "resolver la salida %s tras el reinicio",
                        symbol, intent.reason.value)
                    return

        if pos.reglas.cerrada:
            self._cerrar(pos, ahora)
        else:
            self.abiertas[symbol] = pos
            log.info("bot: %s reconstruida tras reinicio (restante %.2f)",
                     symbol, pos.reglas.restante)

    def _confirmar_registrado(self, pos: PosicionAbierta, intent, registrado) -> None:
        """Confirma al motor una salida que ya se ejecutó en vivo.

        Se usa el `ts` de la INTENCIÓN, no el del fill guardado: el motor casa
        cada confirmación con su intención por motivo y ts, y el instante en que
        la orden se ejecutó de verdad puede no coincidir con el de la vela que
        la disparó. Lo que sí se toma del registro es el PRECIO, que es de lo
        que dependen las reglas (la subida del stop a break-even se decide con
        el precio realmente obtenido).
        """
        precio = registrado["precio"]
        pos.reglas.on_fill(Fill(ts=intent.ts, price=precio,
                                fraction=intent.fraction, reason=intent.reason))
        self._acumular_pnl(pos, precio, intent.fraction, registrado["comision"])

    # --- posiciones vivas ---

    async def _avanzar(
        self, pos: PosicionAbierta, transiciones, precio_de: PrecioDe, ahora: int,
    ) -> None:
        if pos.degradada:
            return  # rota por un fallo previo del broker; no se vuelve a tocar
        precio = precio_de(pos.symbol)
        if precio is None or precio <= 0:
            return  # sin precio observado no se evalúa nada este tick
        vela = CandleRow(ts=ahora, open=precio, high=precio, low=precio,
                         close=precio)
        # el stop vigente ANTES de que este `on_candle` pueda moverlo a
        # break-even: es el nivel contra el que se dispara cualquier STOP que
        # salga de esta misma llamada (ver `_precio_regla_de`).
        stop_vigente = pos.reglas.stop_price
        for intent in pos.reglas.on_candle(vela, tuple(transiciones)):
            await self._ejecutar(pos, intent, precio, ahora, stop_vigente)
        if pos.reglas.cerrada:
            self._cerrar(pos, ahora)

    async def _ejecutar(
        self, pos: PosicionAbierta, intent: ExitIntent, precio: float, ahora: int,
        stop_vigente: float, tardio: bool = False,
    ) -> None:
        """Ejecuta una salida intentando cerrar la cantidad completa, reintentando
        fills parciales.

        El motor de reglas mantiene dos contadores: _comprometido (baja al emitir
        la intención) y _restante (baja al confirmar el fill). Si confirmamos menos
        de lo pedido, quedan desincronizados para siempre, y el motor cerrará de
        menos en adelante, incapaz de llegar a su tamaño objetivo.

        Por eso la política es REINTENTAR: no confirmamos una fracción parcial
        directamente, sino que reintentamos cerrar el resto hasta completarlo o
        agotar intentos. Só si tras los intentos queda resto, confirmamos lo
        ejecutado de verdad y marcamos la posición como degradada. El próximo
        arranque reconciliará la discrepancia.
        """
        cantidad_objetivo = pos.size * intent.fraction
        acumulado = 0.0  # cantidad ejecutada hasta ahora
        comisiones_totales = 0.0
        precio_acumulado = 0.0  # suma de precio * cantidad, para media ponderada

        for intento in range(MAX_INTENTOS_CIERRE):
            # ¿cuánto queda por cerrar?
            falta = cantidad_objetivo - acumulado

            # ¿ya acabamos (con tolerancia)?
            if abs(falta) < TOLERANCIA_CANTIDAD:
                break

            orden = await self._broker.cerrar(
                symbol=pos.symbol, direction=pos.direction, cantidad=falta,
                precio_mercado=precio, ts=ahora,
            )

            acumulado += orden.cantidad
            comisiones_totales += orden.comision
            precio_acumulado += orden.precio * orden.cantidad

        # Calcular la fracción realmente ejecutada y el precio medio ponderado
        ejecutado_total = acumulado
        fraccion_ejecutada = ejecutado_total / pos.size if pos.size > 0 else 0.0
        precio_medio = precio_acumulado / ejecutado_total if ejecutado_total > 0 else precio

        # Confirmar al motor UNA SOLA VEZ con la fracción realmente ejecutada
        pos.reglas.on_fill(Fill(ts=intent.ts, price=precio_medio,
                                fraction=fraccion_ejecutada, reason=intent.reason))

        # Acumular PnL con el precio medio ponderado y las comisiones reales
        self._acumular_pnl(pos, precio_medio, fraccion_ejecutada, comisiones_totales)

        self._repo.registrar_fill(
            pos.id, ts=intent.ts, reason=intent.reason, fraction=fraccion_ejecutada,
            precio_referencia=intent.precio_referencia, precio=precio_medio,
            comision=comisiones_totales, tardio=tardio,
            precio_regla=_precio_regla_de(pos, intent, stop_vigente),
        )

        # Si no conseguimos ejecutar lo que se pedía, marcar como degradada
        falta_final = cantidad_objetivo - ejecutado_total
        if abs(falta_final) > TOLERANCIA_CANTIDAD:
            pos.degradada = True
            self._repo.marcar_degradada(pos.id)
            log.error(
                "bot: %s: cierre parcial %s: pedidos %.6g, ejecutados %.6g, "
                "diferencia %.6g. Motor quedará descuadrado en este arranque; "
                "reconciliación en próximo arranque lo reparará.",
                pos.symbol, intent.reason.value, cantidad_objetivo, ejecutado_total,
                falta_final,
            )

    def _acumular_pnl(
        self, pos: PosicionAbierta, precio: float, fraction: float, comision: float,
    ) -> None:
        """Suma a la posición el PnL bruto y la comisión de una fracción
        cerrada a `precio`.

        La comparten `_ejecutar` (una salida que se manda al broker en vivo) y
        `_confirmar_registrado` (una salida ya ejecutada que se le confirma al
        motor durante la réplica): es la ruta del dinero, y dos copias que
        pudieran divergir es justo el riesgo que no se quiere correr aquí.
        """
        signo = 1.0 if pos.direction is Direction.LONG else -1.0
        cantidad = pos.size * fraction
        bruto = signo * (precio - pos.entry_price) * cantidad
        pos.pnl_acumulado += bruto - comision
        pos.fees_acumuladas += comision

    def _cerrar(self, pos: PosicionAbierta, ahora: int) -> None:
        self._repo.cerrar(pos.id, close_ts=ahora, pnl=pos.pnl_acumulado,
                          fees=pos.fees_acumuladas, max_rank=pos.reglas.max_rank)
        self.portfolio.registrar_cierre(pos.symbol, ahora, pos.pnl_acumulado)
        self.abiertas.pop(pos.symbol, None)
        log.info("bot: cierra %s pnl %.2f (equity %.2f)",
                 pos.symbol, pos.pnl_acumulado, self.portfolio.equity())
