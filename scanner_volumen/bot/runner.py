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

El stop en el exchange (Task 7) es la garantía central de la Fase 3: al
confirmar la apertura se coloca en `reglas.stop_price`, se mueve cuando la
regla lo sube a break-even, y se cancela al cerrar por cualquier motivo. El
stop LOCAL (el que `PositionRules.on_candle` sigue evaluando en cada tick)
no se desactiva por tener uno en el exchange -reacciona antes y cubre el
caso de que la orden remota se haya cancelado sin que nos enteremos-; que
ambos disparen no es un problema porque todos los cierres son reduce-only.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from scanner_volumen.bot.broker import Broker
from scanner_volumen.bot.frenos import Frenos
from scanner_volumen.bot.model import OrdenEjecutada, PosicionAbierta, PosicionExchange
from scanner_volumen.bot.portfolio import LivePortfolio
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.config import BotConfig
from scanner_volumen.models import Direction, State
from scanner_volumen.strategy.entries import es_entrada
from scanner_volumen.strategy.model import (
    CandleRow, ExitIntent, ExitReason, Fill, StrategyParams, TransitionRow,
)
from scanner_volumen.strategy.position import PositionRules, agrupar_por_vela

log = logging.getLogger(__name__)

PrecioDe = Callable[[str], float | None]
# Dado un símbolo que el bot cree abierto pero que ya no está en el exchange,
# el fill real con el que se cerró mientras el proceso estaba caído -o
# `None` si no se pudo recuperar de la historia del exchange. Lo usa
# `reconciliar_con_exchange` (Task 8): nunca se inventa un precio de cierre.
# Asíncrono a propósito: la consulta real al historial de fills de Bitget es
# una llamada de red, y este método ya corre en un contexto `async` que hace
# `await` sobre el broker en el resto del fichero -envolver esa llamada en
# una fachada síncrona no aportaría nada y solo trasladaría la curvatura a
# quien cablee esto contra el exchange de verdad (Task 13).
FillDeCierre = Callable[[str], Awaitable[OrdenEjecutada | None]]

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

# Intentos para colocar el stop en el exchange al confirmar una apertura:
# el inicial más dos reintentos. Es la decisión de diseño central de esta
# tarea -operar apalancado sin que el exchange tenga un stop puesto es
# exactamente el riesgo que esta fase existe para eliminar: si el proceso
# muere con la posición abierta, nada la protege-. Agotados los reintentos
# se cierra la posición a mercado en el acto: una pérdida pequeña y
# realizada es preferible a una posición apalancada sin ninguna red.
MAX_INTENTOS_COLOCAR_STOP = 3

# Espera entre reintentos de colocar el stop, en segundos, creciendo
# linealmente con el número de intento fallido (0.2s tras el 1º, 0.4s tras
# el 2º, ...). Sin esta espera, los tres intentos salen prácticamente
# seguidos: contra un exchange fallando por límite de peticiones o por una
# caída momentánea, los tres fallarían por la misma causa y "reintentar"
# no habría servido de nada. No se espera tras el ÚLTIMO intento fallido:
# en ese punto ya se ha decidido cerrar a mercado, y esperar solo alargaría
# el tiempo que la posición pasa sin ninguna protección.
ESPERA_REINTENTO_COLOCAR_STOP_S = 0.2


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
        fill_de_cierre: FillDeCierre | None = None,
        frenos: Frenos | None = None,
    ) -> None:
        self._params = params
        self._cfg = cfg_bot
        self._repo = repo
        self._broker = broker
        self.portfolio = portfolio
        # Los frenos manuales (Task 10): pérdida diaria máxima y parada de
        # emergencia. `None` en los tests de este módulo y en cualquier
        # construcción anterior a esta tarea -sin frenos configurados,
        # `on_tick` nunca corta entradas por este motivo, igual que antes.
        self._frenos = frenos
        # El proveedor del fill real de un cierre que decidió el exchange
        # por su cuenta -mismo contrato que el parámetro homónimo de
        # `reconciliar_con_exchange`, pero inyectado aquí en el constructor
        # porque `sondear_exchange` (Task 9) lo necesita en cada sondeo
        # periódico, y su firma pública (`posiciones_exchange, ahora`) la
        # fija el llamador de la Task 13 sin margen para colarle un
        # argumento más. `None` en paper (no hay exchange que sondear) y en
        # cualquier construcción que no lo necesite -el resto de tests de
        # este módulo, por ejemplo.
        self._fill_de_cierre = fill_de_cierre
        self.abiertas: dict[str, PosicionAbierta] = {}
        self.transiciones_vistas = 0
        self.cierres_tardios = 0
        # Símbolos que el exchange tiene abiertos y que el bot, al arrancar,
        # no supo explicar como propios (Task 8: reconciliación). Se vetan
        # -no se abre nada nuevo en ellos- durante el resto de la sesión de
        # este proceso: la regla que no se negocia es que el bot nunca cierra
        # con dinero real algo que no entiende, y abrir una entrada NUEVA
        # ahí encima solo empeoraría la confusión. No se persiste: es un
        # veto de esta sesión, y el próximo arranque vuelve a reconciliar
        # desde cero.
        self.simbolos_vetados: set[str] = set()

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

        # (2) evaluar entradas nuevas -salvo que algún freno manual (Task 10:
        # pérdida diaria máxima, parada de emergencia) lo impida. El freno
        # corta la entrada ENTERA de este tick, no transición a transición
        # -a diferencia de los descartes de `LivePortfolio`, que sí se miran
        # símbolo a símbolo-, así que se cuenta una vez por tick bloqueado,
        # no una vez por transición que ni se llega a mirar. El bucle de (1)
        # ya corrió: un freno activo nunca deja de gobernar lo ya abierto.
        motivo_freno = self._frenos.puede_abrir(ahora) if self._frenos else None
        if motivo_freno is not None:
            self._repo.incrementar_contador(self._cfg.modo, motivo_freno)
            return
        for t in transiciones:
            try:
                if not es_entrada(t):
                    continue
                if t.symbol in self.simbolos_vetados:
                    # el exchange tiene esto abierto y la reconciliación no
                    # supo reconocerlo como propio (Task 8): no se toca ni se
                    # abre nada encima mientras dure la sesión.
                    self._repo.incrementar_contador(self._cfg.modo, "simbolo vetado")
                    continue
                if t.price is None or t.price <= 0:
                    # sin precio de señal (símbolo sin vela en curso) no hay
                    # con qué abrir: `repo.abrir` exige `entry_price_senal
                    # NOT NULL`.
                    continue
                precio = precio_de(t.symbol)
                if precio is None or precio <= 0:
                    continue  # sin precio observado no se entra; no es un descarte
                if self.portfolio.evaluar_entrada(t, set(self.abiertas), precio) is None:
                    await self._abrir(t, precio, ahora)
            except Exception:
                # aislar el fallo a esta entrada: no debe llevarse por
                # delante las entradas de los demás símbolos de este tick.
                #
                # OJO: desde que `_abrir` coloca el stop DESPUÉS de
                # confirmar la apertura (Task 7), no toda excepción que
                # llega aquí ocurre antes de confirmar. Si `t.symbol` ya
                # está en `self.abiertas`, la posición se abrió y confirmó
                # de verdad -no hay nada que "descartar"-; lo más probable
                # es que fallara colocar el stop y también el cierre de
                # emergencia que le sigue, y ese camino (`_cerrar_por_fallo_
                # de_stop`) ya la marcó `degradada` y dejó su propio
                # `log.error` explícito, así que aquí solo se registra la
                # traza para depurar. Si NO está en `self.abiertas`, el
                # fallo sí ocurrió antes de confirmar (o `broker.abrir`
                # reventó): la fila quedó reservada con `confirmada=False`
                # (ver `bot.repo`), que es exactamente la huella que la
                # reconciliación (Task 8) sabe leer -no hace falta limpiar
                # nada aquí-, y ahí sí es correcto decir que se descarta.
                if t.symbol in self.abiertas:
                    log.exception(
                        "bot: fallo al abrir %s con la posicion ya "
                        "confirmada y abierta; ver el log de arriba para "
                        "el detalle (no se descarta nada)", t.symbol)
                else:
                    log.exception(
                        "bot: fallo al abrir %s; se descarta esta entrada",
                        t.symbol)

    # --- entradas ---

    async def _abrir(self, t: TransitionRow, precio: float, ahora: int) -> None:
        margin = self.portfolio.margen()
        notional = margin * self._params.apalancamiento
        # Orden invertido a propósito (ver módulo `bot.repo`): se genera el
        # client_oid y se RESERVA la fila antes de mandar la orden. Si el
        # proceso muere justo después de que el broker acepte la orden, sin
        # esta reserva la posición queda abierta en el exchange sin ningún
        # registro local, y la reconciliación (Task 8) la trataría como
        # ajena. `entry_price`/`size` son provisionales -el precio de la
        # señal y el nocional a ese precio, porque ambas columnas son `NOT
        # NULL`- y se sobrescriben con el resultado real en
        # `confirmar_apertura`, más abajo.
        client_oid = f"bot-{uuid4().hex}"
        posicion_id = self._repo.abrir(
            modo=self._cfg.modo, symbol=t.symbol, direction=t.direction,
            entry_ts=ahora, entry_price=t.price, entry_price_senal=t.price,
            margin=margin, notional=notional, size=notional / t.price,
            fee_entrada=0.0, client_oid=client_oid, confirmada=False,
        )
        orden = await self._broker.abrir(
            symbol=t.symbol, direction=t.direction, notional=notional,
            precio_mercado=precio, ts=ahora, client_oid=client_oid,
        )
        self._repo.confirmar_apertura(
            posicion_id, entry_price=orden.precio, size=orden.cantidad,
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
        # el stop del exchange es la garantía central de esta fase: si el
        # proceso muere con la posición abierta, tiene que seguir protegida
        # sin depender de que este código vuelva a arrancar.
        await self._colocar_stop_inicial(self.abiertas[t.symbol], precio, ahora)

    async def _colocar_stop_inicial(
        self, pos: PosicionAbierta, precio: float, ahora: int,
    ) -> None:
        """Coloca en el exchange el stop que la regla ya calculó al abrir
        (`pos.reglas.stop_price`), con la política de reintentos definida en
        `MAX_INTENTOS_COLOCAR_STOP`. Si los agota, cierra la posición a
        mercado: preferimos una pérdida pequeña y realizada a una posición
        apalancada sin ninguna red que la proteja si el proceso muere.
        """
        precio_stop = pos.reglas.stop_price
        for intento in range(1, MAX_INTENTOS_COLOCAR_STOP + 1):
            try:
                stop_id = await self._broker.colocar_stop(
                    symbol=pos.symbol, direction=pos.direction,
                    cantidad=pos.size, precio_disparo=precio_stop,
                    client_oid=f"stop-{uuid4().hex}",
                )
            except Exception:
                log.exception(
                    "bot: fallo al colocar el stop de %s (intento %d/%d)",
                    pos.symbol, intento, MAX_INTENTOS_COLOCAR_STOP,
                )
                if intento < MAX_INTENTOS_COLOCAR_STOP:
                    await asyncio.sleep(ESPERA_REINTENTO_COLOCAR_STOP_S * intento)
                continue
            pos.stop_id = stop_id
            pos.stop_price_colocado = precio_stop
            self._repo.fijar_stop_id(pos.id, stop_id)
            return

        log.error(
            "bot: no se pudo colocar el stop de %s tras %d intentos; se "
            "cierra la posicion a mercado -una perdida pequena y realizada "
            "es preferible a una posicion apalancada sin proteccion",
            pos.symbol, MAX_INTENTOS_COLOCAR_STOP,
        )
        await self._cerrar_por_fallo_de_stop(pos, precio, ahora)

    async def _cerrar_por_fallo_de_stop(
        self, pos: PosicionAbierta, precio: float, ahora: int,
    ) -> None:
        """Cierra la posición entera a mercado porque no se pudo colocar su
        stop en el exchange. No pasa por el motor de reglas -no hay ninguna
        `ExitIntent` que confirmar; es una decisión operativa del runner
        ante un fallo de infraestructura, no una salida que la estrategia
        propusiera-, igual que el cierre por fin de datos del backtest.
        Se registra como un fill de motivo STOP: en espíritu es exactamente
        eso, un stop protegiendo la posición, solo que ejecutado a mercado
        en vez de por una orden stop del exchange que no llegó a existir.

        Si este cierre de emergencia TAMBIÉN falla -la red ya no daba para
        colocar el stop, y tampoco da para el cierre a mercado que debía
        sustituirlo-, la posición queda apalancada, confirmada y sin ningún
        stop en el exchange: exactamente lo que esta tarea existe para
        evitar. No hay nada más que este método pueda intentar en el mismo
        tick, así que no se reintenta aquí -evitaríamos alargar más el
        tiempo sin protección solo para volver a fallar por la misma
        causa-. Se marca `degradada` (persistido, como en el resto de
        caminos) para que `_avanzar` deje de tocar su motor, se mantiene en
        `abiertas` -sigue realmente abierta, ocupando su hueco- y se grita
        en el log con toda claridad: esto exige mirar la cuenta a mano.
        No se relanza la excepción: quien llama (`_abrir`, vía `on_tick`)
        no debe registrar esto como "se descarta esta entrada" -la entrada
        se ejecutó y sigue abierta, es justo lo contrario de descartada-.
        """
        cantidad = pos.size * pos.reglas.restante
        try:
            orden = await self._broker.cerrar(
                symbol=pos.symbol, direction=pos.direction, cantidad=cantidad,
                precio_mercado=precio, ts=ahora,
            )
        except Exception:
            pos.degradada = True
            self._repo.marcar_degradada(pos.id)
            log.error(
                "bot: %s: fallo al colocar el stop Y al cerrar la posicion de "
                "emergencia que debia sustituirlo. POSICION ABIERTA, "
                "APALANCADA Y SIN NINGUN STOP EN EL EXCHANGE -- requiere "
                "intervencion manual inmediata.",
                pos.symbol,
            )
            return
        self._acumular_pnl(pos, orden.precio, pos.reglas.restante, orden.comision)
        self._repo.registrar_fill(
            pos.id, ts=ahora, reason=ExitReason.STOP, fraction=pos.reglas.restante,
            precio_referencia=precio, precio=orden.precio, comision=orden.comision,
            precio_regla=pos.reglas.stop_price,
        )
        await self._cerrar(pos, ahora)

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
        reglas = PositionRules(entrada_real, self._params)
        pos = PosicionAbierta(
            id=fila["id"], symbol=symbol, direction=direccion, entry_ts=entry_ts,
            entry_price=fila["entry_price"],
            entry_price_senal=fila["entry_price_senal"], margin=fila["margin"],
            notional=fila["notional"], size=fila["size"], reglas=reglas,
            pnl_acumulado=-fila["fee_entrada"], fees_acumuladas=fila["fee_entrada"],
            # el `stop_id` de antes del reinicio, si lo hay -una base migrada
            # desde antes de esta tarea no tiene ninguno-. `stop_price_colocado`
            # se ancla al nivel inicial, no al que tenga la regla tras la
            # réplica que sigue abajo: si esa réplica confirma una parcial en
            # beneficio, la regla sube a break-even DURANTE la réplica, y el
            # primer `_avanzar` en caliente tiene que notar ese desajuste
            # contra lo que de verdad hay puesto en el exchange y moverlo -que
            # es exactamente el mismo mecanismo que usa un tick normal, no uno
            # especial para el reinicio.
            stop_id=fila["stop_id"], stop_price_colocado=reglas.stop_price,
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
            await self._cerrar(pos, ahora)
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

    # --- reconciliación con el exchange al arrancar (Task 8) ---

    async def reconciliar_con_exchange(
        self, posiciones_exchange: list[PosicionExchange],
        transiciones_de, velas_de, precio_de: PrecioDe,
        fill_de_cierre: FillDeCierre, ahora: int,
    ) -> None:
        """Reconcilia lo que el bot cree tener abierto contra lo que el
        exchange reporta de verdad. En modo paper la base de datos es la
        verdad; en modo real, la verdad la tiene el exchange -ahí es donde
        está el dinero-, y esto es lo que cierra esa brecha al arrancar.

        Recorre las tres situaciones posibles, en este orden:

        1. Ambos la tienen: se ADOPTA reutilizando `_reconstruir_una` -la
           misma maquinaria de la Fase 2 que replica el historial de
           transiciones y velas y casa los fills ya registrados, dejando el
           motor en su estado exacto.
        2. El bot la cree abierta y el exchange no: se cerró mientras
           estábamos caídos. Se busca el FILL REAL de ese cierre (nunca se
           inventa un precio) y se cierra en la base a ese precio.
        3. El exchange la tiene y el bot no la reconoce: la regla que no se
           negocia es que el bot nunca cierra con dinero real algo que no
           entiende. NO se toca -se deja intacta para que la vea un
           humano- y se veta el símbolo el resto de la sesión
           (`self.simbolos_vetados`, comprobado en `on_tick`).

        Antes de dar una posición del exchange por ajena, se busca por
        `client_oid` entre las filas reservadas sin confirmar
        (`confirmada = 0`): esa es la huella exacta de un proceso que murió
        entre mandar la orden y registrar su resultado, y si coincide la
        posición es propia, no ajena -se confirma con los datos reales y
        sigue el camino normal del punto 1- (ver `_resolver_reserva`). Una
        reserva SIN contrapartida en el exchange significa que la orden
        nunca llegó a ejecutarse: se cierra sin operación para no dejarla
        colgada ocupando un hueco de concurrencia -pero solo cuando esa
        ausencia es concluyente (todas las posiciones del exchange traían
        `client_oid` y ninguna coincidía); si alguna posición del exchange
        vino sin identificador, no se puede descartar que sea justo esa la
        posición propia, y la reserva se deja intacta para revisión manual
        en vez de arriesgar cerrarla por error.

        Cada posición se aísla de las demás, igual que en `on_tick` y en
        `reconstruir`: un fallo al reconciliar una no debe impedir
        reconciliar el resto ni que el bot llegue a arrancar.
        """
        modo = self._cfg.modo

        for fila in self._repo.reservadas_sin_confirmar(modo):
            try:
                self._resolver_reserva(fila, posiciones_exchange, ahora)
            except Exception:
                log.exception(
                    "bot: reconciliacion: fallo al resolver la reserva de "
                    "%s (client_oid=%r); se deja tal cual para el proximo "
                    "arranque", fila["symbol"], fila["client_oid"])

        filas_abiertas = self._repo.abiertas(modo)
        simbolos_bot = {f["symbol"] for f in filas_abiertas}
        por_symbol_exchange = {p.symbol: p for p in posiciones_exchange}

        for fila in filas_abiertas:
            try:
                if not fila["confirmada"]:
                    # sigue reservada: `_resolver_reserva` no pudo ni
                    # confirmarla ni descartarla con certeza (client_oid sin
                    # correlacionar, ver más abajo). Sus datos
                    # (`entry_price`/`size`) son provisionales -no se puede
                    # adoptar ni cerrar con ellos sin arriesgar el libro
                    # contable-, así que se deja tal cual para el próximo
                    # arranque; ya quedó registrada con su propio contador.
                    continue
                if fila["symbol"] in por_symbol_exchange:
                    # (1) ambos la tienen: adoptar reconstruyendo el motor.
                    await self._reconstruir_una(
                        fila, transiciones_de, velas_de, precio_de, ahora)
                else:
                    # (2) el bot la cree abierta, el exchange no.
                    await self._reconciliar_cerrada_en_exchange(
                        fila, transiciones_de, fill_de_cierre, ahora)
            except Exception:
                log.exception(
                    "bot: reconciliacion: fallo al reconciliar %s; se deja "
                    "tal cual para el proximo arranque", fila["symbol"])

        for symbol in por_symbol_exchange:
            if symbol in simbolos_bot:
                continue
            try:
                # (3) el exchange la tiene y el bot no la reconoce: no se
                # toca, se veta.
                self.simbolos_vetados.add(symbol)
                self._repo.incrementar_contador(modo, "posiciones ajenas")
                log.error(
                    "bot: reconciliacion: %s abierta en el exchange y "
                    "desconocida para el bot; NO se toca -- simbolo vetado "
                    "el resto de la sesion, requiere revision manual",
                    symbol)
            except Exception:
                log.exception(
                    "bot: reconciliacion: fallo al registrar %s como ajena",
                    symbol)

    def _resolver_reserva(
        self, fila: dict, posiciones_exchange: list[PosicionExchange],
        ahora: int,
    ) -> None:
        """Resuelve una fila `confirmada = 0`: la huella de un proceso que
        murió entre mandar la orden y registrar su resultado.

        Si su `client_oid` casa con una posición del exchange, la orden SÍ
        se ejecutó -es propia-: se confirma con los datos reales (igual que
        `confirmar_apertura` en caliente) y sigue su camino normal como una
        posición más en `reconciliar_con_exchange` (el símbolo ya aparecerá
        en `repo.abiertas()` confirmado cuando ese paso vuelva a leerla).
        `fee_entrada` se sustituye por la comisión real del exchange cuando
        se conoce; el valor de la reserva es solo el provisional que
        `BotRunner._abrir` graba ANTES de mandar la orden -mantenerlo
        dejaría el PnL de la posición inflado exactamente en lo que costó
        abrir. Si el exchange no la conserva, se avisa con un `log.warning`
        explícito en vez de dejarlo pasar en silencio.

        Si NINGUNA posición del exchange trae este `client_oid`, hay dos
        lecturas posibles y no son intercambiables:

        - Si todas las posiciones del exchange traían identificador (y por
          tanto la ausencia de coincidencia es real, no un artefacto de
          datos incompletos), la orden nunca llegó a ejecutarse: se cierra
          sin operación (pnl y comisión de cierre en cero) para no dejarla
          colgada ocupando un hueco de concurrencia.
        - Si alguna posición del exchange vino SIN identificador (o esta
          misma reserva no tiene `client_oid` que buscar), no hay forma de
          descartar que sea justo esa la posición propia: cerrarla como "no
          ejecutada" podría estar liquidando en el libro contable, con
          pnl=0, una posición que en realidad sigue viva y apalancada en el
          exchange. Se deja la fila intacta -ni confirmada ni cerrada- para
          revisión manual, contada aparte (`"reserva sin correlacionar"`,
          distinta de `"reserva sin ejecutar"` para que el motivo se pueda
          diagnosticar en producción).
        """
        client_oid = fila["client_oid"]
        pos_exch = next(
            (p for p in posiciones_exchange
             if client_oid is not None and p.client_oid == client_oid),
            None,
        )
        if pos_exch is not None:
            fee_entrada = pos_exch.fee_entrada
            if fee_entrada is None:
                fee_entrada = fila["fee_entrada"]
                log.warning(
                    "bot: reconciliacion: %s (client_oid=%r) confirmada sin "
                    "comision real de entrada; se mantiene el valor "
                    "provisional (%.6g) y el PnL de esta posicion quedara "
                    "optimista en esa cantidad", fila["symbol"], client_oid,
                    fee_entrada)
            self._repo.confirmar_apertura(
                fila["id"], entry_price=pos_exch.entry_price,
                size=pos_exch.size, fee_entrada=fee_entrada,
            )
            return

        sin_identificador = any(p.client_oid is None for p in posiciones_exchange)
        if client_oid is None or sin_identificador:
            self._repo.incrementar_contador(
                self._cfg.modo, "reserva sin correlacionar")
            log.error(
                "bot: reconciliacion: %s (client_oid=%r) reservada sin "
                "confirmar, y no se pudo correlacionar con certeza contra "
                "el exchange -hay posiciones sin identificador de orden, o "
                "esta reserva no tiene uno propio-; se deja intacta -- "
                "requiere revision manual", fila["symbol"], client_oid)
            return

        self._repo.cerrar(fila["id"], close_ts=ahora, pnl=0.0,
                          fees=fila["fee_entrada"], max_rank=0)
        self._repo.incrementar_contador(self._cfg.modo, "reserva sin ejecutar")
        log.warning(
            "bot: reconciliacion: %s (client_oid=%r) reservada sin "
            "contrapartida en el exchange; la orden nunca se ejecuto, se "
            "cierra sin operacion", fila["symbol"], client_oid)

    async def _reconciliar_cerrada_en_exchange(
        self, fila: dict, transiciones_de, fill_de_cierre: FillDeCierre,
        ahora: int,
    ) -> None:
        """Cierra en la base una posición que el bot cree abierta pero que
        ya no existe en el exchange: se cerró mientras el proceso estaba
        caído. Se completa SIEMPRE con el fill real de ese cierre -nunca con
        un precio inventado-; si no se encuentra, se deja la fila intacta
        para revisión manual en vez de arriesgar un PnL fantasma.
        """
        symbol = fila["symbol"]
        orden = await fill_de_cierre(symbol)
        if orden is None:
            log.error(
                "bot: reconciliacion: %s figura abierta en la base pero no "
                "en el exchange, y no se encontro el fill real de su "
                "cierre; se deja intacta -- requiere revision manual",
                symbol)
            return

        entry_ts = fila["entry_ts"]
        direction = Direction(fila["direction"])
        historial = list(transiciones_de(symbol, entry_ts))
        entrada = next((t for t in historial if t.ts == entry_ts), None)
        if entrada is not None:
            max_rank = max(t.new_state.rank for t in historial)
            entrada_real = TransitionRow(
                ts=entry_ts, symbol=symbol, prev_state=entrada.prev_state,
                new_state=entrada.new_state, price=fila["entry_price"],
                direction=direction, score=entrada.score,
            )
        else:
            # sin transición de entrada en la base no hay de dónde sacar el
            # `max_rank` real; se usa el mínimo posible en vez de reventar
            # -esto solo maquilla una estadística del informe (Runners vs
            # Arrastre), nunca el dinero, que sale íntegro de `_acumular_pnl`.
            max_rank = 0
            entrada_real = TransitionRow(
                ts=entry_ts, symbol=symbol, prev_state=State.NORMAL,
                new_state=State.NORMAL, price=fila["entry_price"],
                direction=direction, score=0.0,
            )

        # `pos` es una construcción de solo lectura de contabilidad: no se
        # vuelve a tocar su motor de reglas (no hay ninguna `ExitIntent`
        # pendiente que confirmar, el exchange ya decidió el cierre por su
        # cuenta), solo se usa para reutilizar `_acumular_pnl` -la única
        # fórmula de la que sale el PnL real- en vez de recalcularlo aquí
        # con una segunda copia que pudiera divergir.
        pos = PosicionAbierta(
            id=fila["id"], symbol=symbol, direction=direction,
            entry_ts=entry_ts, entry_price=fila["entry_price"],
            entry_price_senal=fila["entry_price_senal"], margin=fila["margin"],
            notional=fila["notional"], size=fila["size"],
            reglas=PositionRules(entrada_real, self._params),
            pnl_acumulado=-fila["fee_entrada"], fees_acumuladas=fila["fee_entrada"],
            stop_id=fila["stop_id"],
        )
        restante = 1.0
        for f in self._repo.fills_de(fila["id"]):
            self._acumular_pnl(pos, f["precio"], f["fraction"], f["comision"])
            restante -= f["fraction"]
        if restante > TOLERANCIA_CANTIDAD:
            self._acumular_pnl(pos, orden.precio, restante, orden.comision)
            # Motivo STOP: en espíritu es exactamente eso -el stop que
            # habíamos dejado puesto en el exchange (Task 7) protegiendo la
            # posición mientras el proceso estaba caído-, con la salvedad de
            # que aquí no hay un nivel de regla conocido contra el que medir
            # el desvío (`precio_regla=None`, igual que EXTREME/END_OF_DATA).
            self._repo.registrar_fill(
                pos.id, ts=ahora, reason=ExitReason.STOP, fraction=restante,
                precio_referencia=orden.precio, precio=orden.precio,
                comision=orden.comision,
            )

        await self._cancelar_stop(pos)
        self._repo.cerrar(pos.id, close_ts=ahora, pnl=pos.pnl_acumulado,
                          fees=pos.fees_acumuladas, max_rank=max_rank)
        self.portfolio.registrar_cierre(symbol, ahora, pos.pnl_acumulado)
        self._repo.incrementar_contador(
            self._cfg.modo, "posiciones cerradas en el exchange")
        log.warning(
            "bot: reconciliacion: %s cerrada en el exchange mientras el bot "
            "estaba caido; se cierra en la base a %.6g (pnl %.2f)",
            symbol, orden.precio, pos.pnl_acumulado)

    # --- sondeo periodico de cierres del exchange (Task 9) ---

    async def sondear_exchange(
        self, posiciones_exchange: list[PosicionExchange], ahora: int,
    ) -> None:
        """Detecta, por sondeo periódico, una posición que el exchange cerró
        por su cuenta -el stop saltó, o hubo liquidación- mientras el bot
        miraba a otro lado: con el stop puesto en el exchange (Task 7) la
        posición sigue protegida aunque el proceso no reaccione al instante,
        pero el motor de reglas seguiría creyéndola abierta hasta que algo
        se lo diga.

        Distinto, y deliberadamente más simple, que `reconciliar_con_
        exchange` (Task 8): aquella corre UNA VEZ al arrancar y cubre las
        tres situaciones posibles (adoptar, cerrar, vetar) porque en ese
        instante el bot no sabe nada todavía. Este método corre
        PERIÓDICAMENTE mientras el bot ya está vivo y gobernando -la Task 13
        cablea su cadencia-, así que las otras dos situaciones no aplican
        aquí: una posición que ambos tienen ya la gobierna `on_tick` en cada
        tick, no hace falta "adoptarla" de nuevo en cada sondeo; y una
        posición que el exchange tiene y el bot no reconoce no es de este
        bot -abrirla no fue su decisión-, tocarla incumpliría la misma regla
        que ya respeta la reconciliación de arranque, y si es genuinamente
        ajena ya quedó vetada al arrancar.

        En modo `paper` no hay exchange real que sondear: no hace nada.

        Cada símbolo se aísla de los demás, igual que en `on_tick` y en
        `reconciliar_con_exchange`: un fallo al cerrar uno no debe impedir
        sondear el resto.
        """
        if self._cfg.modo == "paper":
            return
        simbolos_exchange = {p.symbol for p in posiciones_exchange}
        for symbol in list(self.abiertas):
            if symbol in simbolos_exchange:
                continue
            try:
                await self._cerrar_por_sondeo(symbol, ahora)
            except Exception:
                log.exception(
                    "bot: sondeo: fallo al cerrar %s tras detectar que ya "
                    "no esta en el exchange; se reintentara en el proximo "
                    "sondeo", symbol)

    async def _cerrar_por_sondeo(self, symbol: str, ahora: int) -> None:
        """Cierra en la base una posición que ya no está en el exchange,
        completándola SIEMPRE con el fill real de ese cierre -nunca con un
        precio inventado-. Si no se encuentra (`_fill_de_cierre` es `None`,
        o la consulta no devuelve nada), se deja la posición intacta -sigue
        realmente abierta y protegida por lo que quedara de su stop- para
        que el próximo sondeo, o la reconciliación del próximo arranque, lo
        resuelva con más información; inventar un precio aquí falsificaría
        el libro contable.

        Se registra como un fill de motivo `STOP` -en espíritu es
        exactamente eso, el stop del exchange (o una liquidación)
        protegiendo la posición, solo que el bot no estaba mirando cuando
        ocurrió- con `cierre_exchange=True`: esa columna es la que lo
        distingue de un `STOP` que disparó el motor de reglas en caliente,
        sin ensuciar `ExitReason` (ver `BotRepo.registrar_fill`).

        `pos.reglas.restante` ya refleja lo que de verdad queda abierto -a
        diferencia de `_reconciliar_cerrada_en_exchange`, donde `pos` es una
        reconstrucción de solo lectura sin fills aplicados a su motor, esta
        `pos` es la posición VIVA que `on_tick` lleva gobernando, así que su
        motor ya tiene descontada cualquier parcial cobrada antes de este
        sondeo."""
        pos = self.abiertas[symbol]
        orden = await self._fill_de_cierre(symbol) if self._fill_de_cierre else None
        if orden is None:
            self._repo.incrementar_contador(self._cfg.modo, "sondeo sin fill real")
            log.error(
                "bot: sondeo: %s desaparecio del exchange y no se encontro "
                "el fill real de su cierre; se deja intacta -- requiere "
                "revision manual", symbol)
            return

        restante = pos.reglas.restante
        self._acumular_pnl(pos, orden.precio, restante, orden.comision)
        self._repo.registrar_fill(
            pos.id, ts=ahora, reason=ExitReason.STOP, fraction=restante,
            precio_referencia=orden.precio, precio=orden.precio,
            comision=orden.comision, cierre_exchange=True,
        )
        await self._cerrar(pos, ahora)
        self._repo.incrementar_contador(
            self._cfg.modo, "cierres detectados por sondeo")
        log.warning(
            "bot: sondeo: %s desaparecio del exchange; se cierra en la base "
            "a %.6g (pnl %.2f)", symbol, orden.precio, pos.pnl_acumulado)

    # --- posiciones vivas ---

    async def _avanzar(
        self, pos: PosicionAbierta, transiciones, precio_de: PrecioDe, ahora: int,
    ) -> None:
        if pos.degradada:
            # Rota por un fallo previo del broker; no se vuelve a tocar su
            # motor. Esto incluye el caso de `_cerrar_por_fallo_de_stop`
            # fallando también (`stop_id is None` y `degradada`): se valoró
            # que `_sincronizar_stop` reintentara colocar el stop en ese
            # caso concreto -leer `reglas.stop_price` no mutaría el motor, y
            # técnicamente sería seguro-, pero se descartó: distinguir esa
            # causa de degradación de las demás (un fallo a media ejecución
            # con una intención sin confirmar, donde SÍ sería peligroso
            # tocar el motor) exigiría una señal nueva más allá de
            # `degradada`, y un reintento automático silencioso contra un
            # broker que ya falló cuatro veces seguidas (tres al colocar,
            # una al cerrar) arriesga enmascarar un problema de fondo (claves
            # inválidas, margen insuficiente, símbolo deslistado) que un
            # humano tiene que ver -por eso ese camino termina en un
            # `log.error` explícito en vez de un reintento silencioso.
            return
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
            await self._cerrar(pos, ahora)
        else:
            # el stop local (el que evalúa `on_candle` arriba) sigue
            # activo a propósito -no se desactiva por tener uno en el
            # exchange-: reacciona antes y cubre el caso de que la orden
            # remota se haya cancelado sin que nos enteremos. Que ambos
            # disparen no es un problema porque todos los cierres son
            # reduce-only.
            await self._sincronizar_stop(pos, ahora)

    async def _sincronizar_stop(self, pos: PosicionAbierta, ahora: int) -> None:
        """Refleja en el exchange un movimiento del stop de la regla.

        Hoy el único caso es la subida a break-even (`reglas.stop_en_be`):
        en cuanto una parcial en beneficio la dispara, `reglas.stop_price`
        cambia de valor y se queda ahí para siempre, así que comparar contra
        el último valor colocado (`pos.stop_price_colocado`) basta para
        detectarlo sin necesitar mirar `stop_en_be` directamente.

        Si no hay ningún stop colocado (`pos.stop_id is None`, porque
        colocarlo falló y ya se decidió cerrar la posición, o porque una
        base vieja no lo conoce) no hay nada que mover.

        `mover_stop` SÍ lanza si `stop_id` ya no corresponde a un stop vivo
        -a diferencia de `cancelar_stop`, que es idempotente-: eso puede
        pasar si el stop ya saltó en el exchange justo antes de este tick.
        No es un fallo que deba tumbar el tick ni degradar la posición -el
        stop local sigue vigilando, y el próximo `_avanzar` volverá a
        intentarlo si la posición sigue abierta-, así que se registra y se
        sigue sin reintentar aquí.
        """
        if pos.stop_id is None:
            return
        nuevo_precio = pos.reglas.stop_price
        if nuevo_precio == pos.stop_price_colocado:
            return
        try:
            nuevo_id = await self._broker.mover_stop(
                symbol=pos.symbol, stop_id=pos.stop_id, precio_disparo=nuevo_precio,
            )
        except Exception:
            log.exception(
                "bot: fallo al mover el stop de %s a %.6g; se reintentará en "
                "el siguiente tick", pos.symbol, nuevo_precio,
            )
            return
        pos.stop_id = nuevo_id
        pos.stop_price_colocado = nuevo_precio
        self._repo.fijar_stop_id(pos.id, nuevo_id)

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

    async def _cerrar(self, pos: PosicionAbierta, ahora: int) -> None:
        await self._cancelar_stop(pos)
        self._repo.cerrar(pos.id, close_ts=ahora, pnl=pos.pnl_acumulado,
                          fees=pos.fees_acumuladas, max_rank=pos.reglas.max_rank)
        self.portfolio.registrar_cierre(pos.symbol, ahora, pos.pnl_acumulado)
        self.abiertas.pop(pos.symbol, None)
        # `equity()` aquí es puramente informativo para el log: la posición
        # YA está cerrada en la base y fuera de `self.abiertas` en las tres
        # líneas de arriba, así que un fallo al leerla (Task 11: un
        # `proveedor_saldo` que devuelve un valor inválido lanza
        # `ValueError`) NUNCA debe propagarse desde aquí. Si se dejara
        # propagar, el `try/except` de `on_tick` que gobierna
        # `self.abiertas` (línea ~184) lo capturaría y marcaría esta MISMA
        # posición -ya cerrada- como `degradada`: un estado sin sentido
        # para una fila que ya no está abierta, y que el operador leería
        # como una posición viva sin gobierno cuando en realidad ya se
        # liquidó correctamente (hallazgo de revisión). Aislado aquí en vez
        # de arriba porque el problema es específico de este log, no del
        # cierre en sí.
        try:
            equity = self.portfolio.equity()
        except Exception:
            log.exception(
                "bot: cierra %s pnl %.2f (no se pudo leer el equity para "
                "este log; la posicion SI quedo cerrada correctamente)",
                pos.symbol, pos.pnl_acumulado,
            )
            return
        log.info("bot: cierra %s pnl %.2f (equity %.2f)",
                 pos.symbol, pos.pnl_acumulado, equity)

    async def _cancelar_stop(self, pos: PosicionAbierta) -> None:
        """Cancela el stop del exchange al cerrar la posición, por cualquier
        motivo -incluido que haya sido el propio stop el que la cerró-.

        `cancelar_stop` es idempotente por contrato del broker: no lanza si
        el stop ya no existe, que es el caso normal cuando fue él mismo
        quien disparó el cierre. Si aun así falla (un fallo real de red o
        del exchange), se registra pero no impide dar la posición por
        cerrada -está cerrada de verdad, con el dinero ya liquidado; dejarla
        `abierta = 1` en la base de datos por esto la dejaría atascada para
        siempre, ocupando su hueco de concurrencia sin que nada la vuelva a
        gobernar. Un stop huérfano en el exchange, si llega a pasar, es lo
        que la reconciliación de arranque tiene que encontrar y limpiar.
        """
        if pos.stop_id is None:
            return  # nunca se colocó, o ya se limpió (no hay nada que cancelar)
        try:
            await self._broker.cancelar_stop(symbol=pos.symbol, stop_id=pos.stop_id)
        except Exception:
            log.exception("bot: fallo al cancelar el stop de %s al cerrar", pos.symbol)
