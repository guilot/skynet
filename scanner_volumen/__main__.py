# scanner_volumen/__main__.py
"""Punto de entrada: arranca todas las tareas del scanner.

Es el único sitio donde el reloj de pared (`time.time_ns`, vía `ahora_ms`) se
lee directamente. Pero I6 lo acota más: en cuanto llega el primer ticker,
`Orchestrator.now_ms` empieza a devolver el reloj del exchange (el ts que
Bitget estampa en cada respuesta de `/tickers`) y `ahora_ms()` deja de
influir en nada -queda solo como respaldo para el arranque en frío, antes de
que exista ningún ticker-. Todos los bucles de abajo llaman a
`orq.now_ms(ahora_ms())`, nunca a `ahora_ms()` a secas, para que "ahora"
signifique lo mismo en todo el proceso y esa sea la única frontera legítima
con el reloj de pared (spec §13: "nunca la hora local").

El orquestador ya expone `poll_tickers` y `evaluate`, ambos probados, pero no
un bucle que también refresque el universo: `poll_tickers` solo actualiza los
símbolos que ya tienen buffer, así que la selección de universo (que necesita
la lista completa de tickers, incluidos los símbolos aún no vistos) se hace
aparte, con su propia cadencia (`universe.refresh_minutes`). Igualmente,
`evaluate` no drena `reconnected` ni llama a `refill_gap`: eso se hace aquí,
en el bucle evaluador, para que una reconexión tras una caída larga no deje
un hueco silencioso en el histórico.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import uvicorn

from scanner_volumen.api.server import create_app
from scanner_volumen.app.bootstrap import Bootstrapper
from scanner_volumen.app.orchestrator import Orchestrator
from scanner_volumen.app.outcomes import OutcomeTracker
from scanner_volumen.bitget.private import BitgetPrivate
from scanner_volumen.bitget.private import PosicionExchange as PosicionExchangeBitget
from scanner_volumen.bitget.rest import BASE_URL, BitgetRest
from scanner_volumen.bitget.ws import BitgetWebsocket
from scanner_volumen.bot.bitget_broker import BitgetBroker
from scanner_volumen.bot.broker import Broker, PaperBroker
from scanner_volumen.bot.frenos import Frenos
from scanner_volumen.bot.model import PosicionExchange
from scanner_volumen.bot.modo import PAPER, REAL, REAL_LECTURA, resolver_modo
from scanner_volumen.bot.portfolio import LivePortfolio
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.bot.runner import BotRunner
from scanner_volumen.bot.verificacion_cuenta import VerificadorCuenta
from scanner_volumen.config import Config, load_config
from scanner_volumen.models import Direction, State
from scanner_volumen.provenance import get_code_revision
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import (
    CandleRepo, MaintenanceRepo, ProfileRepo, SignalRepo, StateTransitionRepo,
    SupplyRepo,
)
from scanner_volumen.strategy.model import CandleRow, StrategyParams, TransitionRow
from scanner_volumen.universe.selector import UniverseSelector
from scanner_volumen.universe.supply import SupplyCache

log = logging.getLogger("scanner")


def ahora_ms() -> int:
    return time.time_ns() // 1_000_000


# --- cuerpos de los bucles principales, extraídos como corrutinas con nombre ---
#
# I-5: `__main__.py` no tenía ninguna cobertura, y ahí vivieron los defectos
# de esta fase (los call sites de `orq.now_ms(ahora_ms())`, el bucle de
# mantenimiento, `_marcar_ws_conectado`, `stale_after_ms` y el cableado de
# `OutcomeTracker(horizons=...)`). Cada bucle de `main()` era antes un
# `while True` anidado con el cuerpo de un solo paso inline: imposible de
# ejercitar sin levantar `httpx.AsyncClient`, `uvicorn` y un `main()` entero.
# Extraer el cuerpo de cada paso a una función con nombre, a nivel de módulo
# y con sus dependencias como parámetros explícitos, permite probarlas con
# dobles de prueba (ver tests/test_main.py) sin tocar red ni reloj de pared
# real. `main()` sigue siendo el único sitio que arma el `while True` con su
# `asyncio.sleep` de cadencia -eso no aporta nada probarlo por separado-.


async def paso_tickers(
    orq: Orchestrator,
    rest: BitgetRest,
    selector: UniverseSelector,
    bootstrapper: Bootstrapper,
    supply: SupplyCache,
    ahora: int,
    ultimo_universo: int,
    refresh_minutes: float,
) -> int:
    """Un paso de `bucle_tickers`: refresca tickers y, si toca, el universo.

    El refresco de ticker en sí se delega en `orq.poll_tickers` (probado
    aparte): marca sucios los símbolos que ya tienen buffer y nunca propaga
    un fallo de REST. La selección de universo no puede delegarse igual
    porque necesita la lista completa de tickers -incluidos símbolos que el
    orquestador todavía no conoce- que `poll_tickers` no expone.

    Devuelve el nuevo `ultimo_universo`: el `while True` de `bucle_tickers`
    lo hace persistir entre iteraciones, así que el llamador es quien debe
    quedarse con el valor devuelto. Un fallo de REST (tickers o universo) se
    registra y deja `orq.state.connected` en `False`; nunca propaga, para
    que un solo ciclo fallido no tumbe el bucle entero.
    """
    try:
        await orq.poll_tickers(ahora)
        if ahora - ultimo_universo >= refresh_minutes * 60_000:
            contratos = await rest.get_contracts()
            tickers_universo = await rest.get_tickers()
            actualizacion = selector.select(contratos, tickers_universo, ahora)
            bootstrapper.expect(actualizacion.ordered)
            await orq.apply_universe(actualizacion, ahora)
            await supply.refresh(actualizacion.ordered, ahora)
            ultimo_universo = ahora
        orq.state.connected = True
    except Exception as exc:  # noqa: BLE001
        orq.state.connected = False
        log.warning("actualización de universo fallida: %s", exc)
    return ultimo_universo


def _precio_de(orq: Orchestrator):
    """Último precio observado de un símbolo: la vela en curso del WebSocket
    (lo más fresco que hay) y, si no la hay, el ticker de REST."""
    def precio(symbol: str) -> float | None:
        buffer = orq.buffers.get(symbol)
        if buffer is not None:
            actual = buffer.current()
            if actual is not None:
                return actual.close
            cerradas = buffer.closed(1)
            if cerradas:
                return cerradas[-1].close
        ticker = orq.tickers.get(symbol)
        return None if ticker is None else ticker.last
    return precio


async def paso_evaluador(
    orq: Orchestrator, bootstrapper: Bootstrapper, ahora: int, bot=None,
    cerrojo: asyncio.Lock | None = None,
) -> None:
    """Un paso de `bucle_evaluador`: drena reconexiones, evalúa y refresca
    el progreso del bootstrap. Ver `paso_tickers` sobre por qué está
    extraído como función con nombre (I-5).

    `bot` es opcional (`None` por defecto) para que con `bot.enabled = false`
    -el valor por defecto de `config.toml`- el escáner se comporte
    exactamente como antes de que existiera el bot: todas las llamadas
    existentes a `paso_evaluador` siguen funcionando sin cambios.

    `cerrojo` es el mismo `asyncio.Lock` que toma `paso_sondeo`: sin él, el
    sondeo del exchange puede pillar a `on_tick` suspendido en un `await` del
    broker y registrar un cierre DUPLICADO (ver `paso_sondeo` para el
    escenario completo). `main()` lo pasa SIEMPRE, también en `paper`, donde
    no hay bucle de sondeo con el que competir: tomar un cerrojo libre no
    cede el control del bucle de eventos (`Lock.acquire` tiene camino rápido
    sin `await`), así que el comportamiento de `paper` no cambia, y una sola
    forma de llamada es menos frágil que dos. El parámetro sigue siendo
    opcional por los llamadores de los tests, anteriores a esta tarea."""
    orq.state.now_ms = ahora  # I-2(a): "ahora" del exchange, para el dashboard
    # Igual que `paso_outcomes`: un fallo transitorio (p. ej. un error de
    # SQLite al persistir una señal o una transición de estado, o un fallo
    # del propio bot) no debe tumbar el bucle del evaluador. Con el registro
    # de transiciones WATCH+ hay muchas más escrituras por hora, así que el
    # bucle necesita esta red.
    try:
        for simbolo in list(orq.reconnected):
            orq.reconnected.discard(simbolo)
            await orq.refill_gap(simbolo, ahora)
        for t in orq.evaluate(ahora):
            if t.should_alert:
                log.info("ALERTA %s %s score=%.1f", t.symbol, t.current.value, t.score)
        if bot is not None:
            try:
                # El cerrojo envuelve SOLO el tick del bot: `orq.evaluate` y
                # el bootstrap no comparten estado con el sondeo y no tienen
                # por qué esperarlo.
                async with (cerrojo if cerrojo is not None
                            else contextlib.nullcontext()):
                    await bot.on_tick(
                        orq.transiciones_evaluadas, _precio_de(orq), ahora)
            except Exception:
                log.exception("bot: fallo aislado en on_tick")
        hecho, total = bootstrapper.progress()
        orq.state.bootstrap_done, orq.state.bootstrap_total = hecho, total
    except Exception as exc:  # noqa: BLE001
        log.warning("paso del evaluador fallido: %s", exc)


async def paso_outcomes(tracker: OutcomeTracker, ahora: int) -> None:
    """Un paso de `bucle_outcomes`. Ver `paso_tickers` (I-5)."""
    try:
        tracker.run_once(ahora)
    except Exception as exc:  # noqa: BLE001
        log.warning("seguimiento de resultados fallido: %s", exc)


# Ms de reintento cuando el mantenimiento está vencido pero no logró hacer
# trabajo real (ver Orchestrator.run_maintenance: típicamente un arranque
# en frío en el que el universo todavía se está poblando, `self.profiles`
# vacío o solo con placeholders). Sin este respiro, `bucle_mantenimiento`
# reintentaría en cada vuelta sin dormir -un busy-loop- en vez de esperar
# un tramo razonable a que el bootstrap de fondo avance.
MANTENIMIENTO_REINTENTO_MS = 60_000


async def paso_mantenimiento(
    orq: Orchestrator, maintenance_repo: MaintenanceRepo, ahora: int, interval_hours: float,
) -> int:
    """Un paso de `bucle_mantenimiento` (I2 poda + I4 recálculo). Ver
    `paso_tickers` (I-5) sobre por qué está extraído como función con
    nombre a nivel de módulo.

    A diferencia de los demás `paso_*`, este no solo ejecuta el trabajo:
    también decide SI toca ejecutarlo y devuelve cuántos ms debe dormir el
    llamador antes de volver a invocarlo. Antes, `bucle_mantenimiento`
    dormía `interval_hours` ANTES de hacer nada; bajo systemd con
    `Restart=always`, un proceso que se reinicia antes de acumular esas
    horas seguidas de vida nunca llegaba a correr mantenimiento -medido en
    real: los perfiles de volumen más viejos llevaban seis días sin
    recalcularse (el propio denominador del RVOL), y la poda de
    `candles_1m` nunca corrió-.

    La decisión de "toca o no" se lee de `maintenance_repo`
    (`MaintenanceRepo`, tabla `maintenance_meta`), que sobrevive a un
    reinicio del proceso -no de cuánto lleva vivo el proceso actual, que es
    justo lo que systemd resetea en cada reinicio-. Un mantenimiento que no
    completó ningún trabajo real (`run_maintenance` devuelve `False`; ver
    su docstring sobre el no-op de arranque en frío) NO estampa la marca:
    seguiría vencido, así que este método devuelve
    `MANTENIMIENTO_REINTENTO_MS` en vez de las `interval_hours` completas,
    para que el llamador reintente pronto sin caer en un busy-loop.
    """
    intervalo_ms = int(interval_hours * 3600_000)
    ultimo = maintenance_repo.get_last_completed_ms()
    vencido = ultimo is None or ahora - ultimo >= intervalo_ms
    if not vencido:
        return ultimo + intervalo_ms - ahora

    try:
        hizo_trabajo = await orq.run_maintenance(ahora)
    except Exception as exc:  # noqa: BLE001
        log.warning("mantenimiento diario fallido: %s", exc)
        hizo_trabajo = False

    if hizo_trabajo:
        maintenance_repo.set_last_completed_ms(ahora)
        return intervalo_ms

    return MANTENIMIENTO_REINTENTO_MS


# --- el bot en los tres modos: piezas, saldo real y sondeo (Task 13) ---
#
# Aquí vive el CABLEADO del bot, no su lógica: las piezas de las tareas 1-12
# ya están construidas y probadas por separado, y este bloque es el único
# sitio donde se juntan. Es también el punto donde una confusión pone dinero
# real en juego, así que cada pieza que solo tiene sentido en un modo se
# construye en una función con nombre, que se puede probar por separado (ver
# `tests/test_main.py`), en vez de en un `if` largo dentro de `main()`.

# Las credenciales de la subcuenta viven SIEMPRE en el entorno del servicio
# (`/etc/scanner_volumen.env`, permisos 600, ver `deploy/ENTORNO.md`), nunca
# en el repositorio ni en el fichero de la unidad systemd -que es legible con
# `systemctl cat`-. Los nombres de las variables de DEMO son otros
# (`SCANNER_BITGET_DEMO_*`, ver `tests/integracion_bitget/seguridad.py`), y
# que no sean dos variantes del mismo nombre es deliberado: si se llamaran
# igual, un copiar y pegar entre ficheros de entorno acabaría operando la
# cuenta real con las claves de prueba, o al revés.
VARIABLE_CLAVE = "SCANNER_BITGET_KEY"
VARIABLE_SECRETO = "SCANNER_BITGET_SECRET"
VARIABLE_PASSPHRASE = "SCANNER_BITGET_PASSPHRASE"
VARIABLES_CREDENCIALES = (VARIABLE_CLAVE, VARIABLE_SECRETO, VARIABLE_PASSPHRASE)

# Cuántos refrescos seguidos puede fallar la lectura del saldo real antes de
# que `ProveedorSaldo` deje de dar por bueno el último valor observado. No es
# un ajuste fino: es la respuesta a "¿cuánto rato puede el freno de pérdida
# diaria medir sobre una cifra que ya no sabemos si es cierta?". Con la
# cadencia por defecto (30 s) son 3 minutos de tolerancia a un fallo
# transitorio de red, y a partir de ahí el proveedor lanza -lo que corta las
# entradas nuevas sin tocar el gobierno de lo ya abierto, la misma doctrina
# que ya siguen los dos frenos (`bot/frenos.py`: ante la duda, se frena).
CICLOS_TOLERADOS_SIN_SALDO = 6


@dataclass(frozen=True)
class PiezasDelBot:
    """Lo que el modo efectivo decide: con qué se ejecuta y con qué se lee.

    `privado` es `None` en `paper` y solo en `paper`: es la señal, en un solo
    campo, de si este proceso tiene credenciales cargadas. `broker` es quien
    manda (o simula) las órdenes.
    """

    modo: str
    broker: Broker
    privado: BitgetPrivate | None


def construir_piezas_del_bot(
    modo: str, params: StrategyParams, cfg: Config,
    http: httpx.AsyncClient, entorno: Mapping[str, str],
) -> PiezasDelBot:
    """La factoría del interruptor del dinero: qué broker y qué cliente se
    construyen para cada uno de los tres modos efectivos (`bot/modo.py`).

    - `paper`: `PaperBroker` y NINGÚN cliente autenticado. Ni siquiera se
      leen las credenciales del entorno -el camino de paper no debe depender
      de que existan, ni cargarlas sin necesitarlas (spec §4).
    - `real_lectura`: cliente autenticado (saldo, posiciones y configuración
      de cuenta reales) pero `PaperBroker` para EJECUTAR. Este es el escalón
      intermedio de toda la fase: se conecta de verdad y no manda ni una
      orden. El test que fija esta línea es el que impide que alguien
      "simplifique" el escalón y empiece a mandar órdenes reales creyendo que
      no.
    - `real`: cliente autenticado y `BitgetBroker`. Aquí sí se mueve dinero.

    Se reutiliza el `httpx.AsyncClient` del escáner a propósito: `BitgetPrivate`
    construye URLs absolutas contra el mismo host que el cliente público
    (`bitget/private.py`, `bitget/rest.py`: el mismo `BASE_URL`), así que un
    segundo cliente solo añadiría otro pool de conexiones y otro `async with`
    que cerrar. Las credenciales viajan en cabeceras por petición, nunca en el
    cliente.
    """
    if modo == PAPER:
        return PiezasDelBot(modo=modo, broker=PaperBroker(params), privado=None)

    privado = _cliente_autenticado(cfg, http, entorno)
    if modo == REAL_LECTURA:
        return PiezasDelBot(modo=modo, broker=PaperBroker(params), privado=privado)
    if modo == REAL:
        return PiezasDelBot(
            modo=modo, broker=BitgetBroker(privado, params), privado=privado)
    # Inalcanzable vía `resolver_modo`, que ya falla cerrado ante cualquier
    # valor que no sea uno de los tres. Se repite la guarda aquí porque esta
    # función decide QUÉ BROKER se usa: un modo nuevo añadido en el futuro
    # debe hacerla fallar, nunca caer por defecto en el broker de dinero real.
    raise ValueError(f"modo efectivo desconocido: {modo!r}")


def _cliente_autenticado(
    cfg: Config, http: httpx.AsyncClient, entorno: Mapping[str, str],
) -> BitgetPrivate:
    """Construye el cliente privado con las credenciales del entorno.

    El mensaje de error nombra las VARIABLES que faltan, jamás su contenido:
    una clave no puede aparecer en un log, en un `repr` ni en una excepción
    (spec §4), y este es justo el sitio donde la tentación de "ayudar a
    depurar" imprimiendo lo que llegó sería más natural.
    """
    faltan = [v for v in VARIABLES_CREDENCIALES if not entorno.get(v)]
    if faltan:
        raise ValueError(
            "los modos reales exigen las credenciales de la subcuenta en el "
            f"entorno del servicio; faltan o vienen vacías: {', '.join(faltan)} "
            "(ver deploy/ENTORNO.md)"
        )
    return BitgetPrivate(
        cfg.market.venue, cfg.rest.rate_limit_per_second, http,
        api_key=entorno[VARIABLE_CLAVE],
        api_secret=entorno[VARIABLE_SECRETO],
        passphrase=entorno[VARIABLE_PASSPHRASE],
    )


class ProveedorSaldo:
    """El puente entre el saldo real de Bitget (asíncrono) y los dos
    consumidores que lo esperan síncrono: `LivePortfolio` (dimensiona el
    margen de cada entrada) y `Frenos` (mide la pérdida diaria).

    **Por qué no puede ser un simple caché que empieza en cero.** Un `0.0` -o
    un `nan`- devuelto en la primera consulta del día se persiste como
    referencia del freno de pérdida diaria en `bot_meta` y lo DESACTIVA el
    resto del día UTC, sobreviviendo a un reinicio (es el fallo que la Task 11
    tuvo que arreglar dos veces). Ya hay guardas que rechazan un valor
    inválido en las dos puntas, pero un freno de seguridad no debe apoyarse en
    que alguien río abajo lo salve: aquí el diseño es que el proveedor **no
    tenga forma de devolver un número que no sea un saldo realmente
    observado**. No existe valor inicial: mientras no haya una lectura buena,
    `__call__` LANZA en vez de inventarse una cifra. Y `refrescar` valida
    antes de guardar, así que un valor espurio del exchange nunca llega a
    entrar en el caché.

    Lanzar no es un problema para ninguno de los dos consumidores, y ese es
    el motivo de elegir esta vía y no "devolver el último valor y confiar en
    las guardas": `LivePortfolio.equity()` ya está documentado como algo que
    puede lanzar (descarta esa entrada), y en `Frenos._saldo_actual` una
    excepción corta las entradas nuevas del tick SIN tocar el gobierno de las
    posiciones abiertas -que ya ha corrido antes en `on_tick`. Es decir: sin
    saldo fiable el bot deja de abrir, pero sigue cuidando lo que tiene
    abierto. Exactamente la doctrina de los frenos.

    **Y hay UN SOLO proveedor** para `LivePortfolio` y para `Frenos`. Si cada
    uno tuviera el suyo, la referencia y la medida del freno volverían a salir
    de fuentes distintas: es el fallo grave que la Task 11 reprodujo (el freno
    midiendo un 4,5% de pérdida con la cuenta un 7% abajo y el tope en el 5%).

    **La vejez también cuenta.** Un saldo observado hace media hora es un
    saldo real, pero ya no es una medida: si la lectura lleva
    `edad_maxima_s` sin refrescarse, se trata como si no la hubiera. Sin esto,
    un corte de red prolongado dejaría al freno de pérdida diaria comparando
    contra una foto congelada -midiendo, sin saberlo, la cuenta de hace un
    rato- mientras la de verdad se hunde. El reloj es inyectable y monótono a
    propósito: aquí se mide un TRANSCURSO local, no un instante de mercado,
    así que ni el reloj del exchange ni un salto de NTP deben poder alterarlo.
    """

    def __init__(
        self, privado: BitgetPrivate, edad_maxima_s: float,
        reloj: Callable[[], float] = time.monotonic,
    ) -> None:
        self._privado = privado
        self._edad_maxima_s = edad_maxima_s
        self._reloj = reloj
        self._saldo: float | None = None
        self._observado_en: float = 0.0

    async def refrescar(self) -> float:
        """Lee el saldo real de la subcuenta y lo guarda como el valor vigente.

        Devuelve el saldo observado -el llamador lo necesita para persistirlo
        con `BotRepo.set_saldo_real` (contrato con esta tarea) sin volver a
        preguntarle al exchange por el mismo número.

        Un saldo no finito o no positivo se rechaza con `ValueError` y NO
        sustituye al último valor bueno: es la única puerta de entrada del
        caché, y por eso es aquí donde se valida y no en quien lo consume.
        """
        saldo = (await self._privado.get_saldo()).realizado
        if not math.isfinite(saldo) or saldo <= 0:
            raise ValueError(
                f"Bitget devolvió un saldo realizado inválido: {saldo!r} (se "
                f"esperaba un número finito y positivo)"
            )
        self._saldo = saldo
        self._observado_en = self._reloj()
        return saldo

    def __call__(self) -> float:
        """El saldo vigente, o `ValueError` si no hay ninguno que merezca ese
        nombre. Es la firma síncrona (`Callable[[], float]`) que esperan
        `LivePortfolio` y `Frenos`.

        Se usa `ValueError` -y no un tipo propio- porque es exactamente lo que
        `LivePortfolio.equity()` ya lanza ante un saldo inválido: los
        llamadores no tienen que distinguir "el proveedor no tiene saldo" de
        "el saldo que dio no vale", que para ellos son la misma situación.
        """
        if self._saldo is None:
            raise ValueError(
                "todavía no se ha observado ningún saldo real de la "
                "subcuenta; no hay ninguna cifra sobre la que dimensionar ni "
                "medir nada"
            )
        edad = self._reloj() - self._observado_en
        if edad > self._edad_maxima_s:
            raise ValueError(
                f"el último saldo real observado tiene {edad:.0f}s, más de "
                f"los {self._edad_maxima_s:.0f}s tolerados; se trata como si "
                f"no hubiera saldo en vez de medir sobre una foto vieja"
            )
        return self._saldo


def posiciones_del_bot(
    posiciones: list[PosicionExchangeBitget],
) -> list[PosicionExchange]:
    """Traduce las posiciones que reporta el cliente privado al vocabulario
    del bot (`bot/model.py`), que es el que entienden `reconciliar_con_
    exchange` y `sondear_exchange`.

    Dos campos no se pueden rellenar con lo que devuelve
    `/api/v2/mix/position/all-position` y se dejan explícitamente vacíos en
    vez de inventarlos:

    - **`client_oid`: el endpoint de posiciones no lo trae, y esto TIENE
      consecuencias** (corregido tras un hallazgo de revisión que demostró
      que la afirmación anterior aquí -"la consecuencia está prevista y es la
      conservadora"- era FALSA). Sin `client_oid`,
      `BotRunner._resolver_reserva` no puede correlacionar NUNCA: una reserva
      sin confirmar -la huella de un proceso que murió entre mandar la orden y
      registrarla, plausible bajo `Restart=always`- no se adopta jamás. Y como
      el stop se coloca DESPUÉS de confirmar la apertura, esa posición real
      puede estar apalancada y sin stop en el exchange. Lo conservador de
      verdad, y lo que ahora hace `_resolver_reserva`, es **vetar el símbolo**:
      antes de ese arreglo el bot abría otra posición encima de la real, en el
      mismo arranque y otra vez en cada reinicio.
    - `entry_ts`: tampoco viene, y ningún camino de la reconciliación lo lee
      (se comprobó uno a uno); se pone a 0 antes que un instante inventado que
      alguien pudiera tomar por real más adelante.

    Se DESCARTAN las posiciones de tamaño no positivo. El motivo es un
    SUPUESTO SIN VERIFICAR (el nº11 de la lista de la Task 12, pendiente de
    confirmar contra la cuenta de simulación): que este endpoint devuelve
    filas con `total = 0` para símbolos sin posición viva, en vez de omitir
    esos símbolos. **El filtro es seguro se comporte como se comporte
    Bitget** -si nunca devolviera filas así, no descarta nada-, y por eso se
    aplica pese a no estar confirmado: una fila así traducida sería una
    posición fantasma con dos efectos contrarios y ambos malos -el sondeo
    creería que la posición sigue abierta y nunca detectaría su cierre, y la
    reconciliación de arranque la vetaría como ajena.

    Un lado que no sea "long" ni "short" lanza en vez de elegir una dirección
    por defecto: interpretar mal el lado de una posición apalancada es peor
    que no interpretarlo, y quien llama a esto trata el fallo como "no se pudo
    leer el estado del exchange" -que es la verdad.
    """
    traducidas = []
    for p in posiciones:
        if p.tamano <= 0:
            continue
        if p.lado == "long":
            direction = Direction.LONG
        elif p.lado == "short":
            direction = Direction.SHORT
        else:
            raise ValueError(
                f"posición de {p.symbol!r} con lado desconocido: {p.lado!r} "
                f"(se esperaba 'long' o 'short')"
            )
        traducidas.append(PosicionExchange(
            symbol=p.symbol, direction=direction, size=p.tamano,
            entry_price=p.precio_entrada, entry_ts=0,
        ))
    return traducidas


async def fill_de_cierre_no_disponible(symbol: str) -> None:
    """El proveedor del fill real de un cierre que decidió el exchange... que
    esta tarea NO puede construir todavía, y por eso dice que no en vez de
    inventar un precio.

    `BotRunner` (tareas 8 y 9) pide este dato cuando descubre que una posición
    que creía abierta ya no está en el exchange, y por diseño NUNCA inventa el
    precio de cierre: si no se consigue el fill real, deja la posición abierta,
    la marca `degradada`, la cuenta (`"sondeo sin fill real"`) y grita en el
    log pidiendo revisión manual. Este proveedor devuelve `None` siempre, así
    que ese es exactamente el camino que se toma.

    **LA CONSECUENCIA OPERATIVA NO ES MENOR** (corregido tras la ronda de
    revisión, que cuantificó lo que la primera versión de este docstring
    despachaba como "ruidoso"): la salida por stop es la salida NORMAL de la
    estrategia, así que esto pasa a menudo. Cada posición varada sigue
    ocupando su hueco de concurrencia; con `max_concurrentes = 5`, cinco de
    ellas dejan al bot sin abrir nada, y sobreviven al reinicio. Por eso el
    informe tiene ahora una línea propia para contarlas
    (`report._lineas_modo_real`) y `deploy/ENTORNO.md` le pide al operador que
    la vigile a diario: la alternativa -inventar un precio- falsificaría el
    libro contable en silencio, que es peor, pero "no falsificar" no es lo
    mismo que "no hace falta hacer nada".

    **Por qué no se implementa aquí.** `BitgetPrivate.get_fill` consulta los
    fills DE UNA ORDEN (`symbol` + `orderId`), y en este escenario no hay
    orderId: la orden la ejecutó el exchange por su cuenta (saltó el stop, o
    hubo liquidación). Resolverlo de verdad exige una consulta nueva al
    historial de fills por símbolo y una regla para elegir cuál de ellos es el
    cierre buscado -es decir, endpoint, forma de respuesta y semántica nuevos,
    ninguno verificado contra la API real (los diez supuestos de la Task 12
    siguen sin confirmar porque hacen falta claves de demo). Escribir esa
    heurística a ciegas significaría arriesgarse a escribir en el libro
    contable el precio de un fill que no es, que es peor que no escribir
    ninguno: un precio equivocado no se distingue después de uno correcto,
    mientras que una posición marcada para revisión manual salta a la vista.
    """
    return None


async def paso_saldo(proveedor: ProveedorSaldo, repo: BotRepo, modo: str) -> None:
    """Un paso de `bucle_saldo`: refresca el saldo real y lo persiste.

    Las dos cosas en el mismo paso, y con el MISMO número, por el contrato
    escrito en `BotRepo.set_saldo_real`: lo que el informe enseña como "saldo
    real" tiene que ser exactamente la cifra que dimensiona el margen y mide
    el freno, no una segunda lectura hecha por otro camino que podría diferir.
    (El contrato hablaba de "cada tick"; se hace en cada refresco, que es
    donde el valor se observa: escribir en cada tick el mismo número que ya
    está en la base no lo haría más fresco, solo añadiría escrituras.)

    No propaga: un fallo de red aquí no debe tumbar el proceso. Lo que sí hace
    es dejar el caché del proveedor sin actualizar, y de eso se encarga el
    propio proveedor -pasada su tolerancia de vejez, deja de dar por bueno el
    último valor y el bot para de abrir. Ver `ProveedorSaldo`.
    """
    try:
        saldo = await proveedor.refrescar()
        repo.set_saldo_real(modo, saldo)
    except Exception as exc:  # noqa: BLE001
        log.warning("bot: no se pudo refrescar el saldo real: %s", exc)


async def paso_sondeo(
    bot: BotRunner, privado: BitgetPrivate, cerrojo: asyncio.Lock, ahora: int,
) -> None:
    """Un paso de `bucle_sondeo`: pregunta al exchange qué posiciones siguen
    vivas y deja que el runner cierre en la base las que ya no están (Task 9).

    **EL CERROJO ES LA RAZÓN DE SER DE ESTA FUNCIÓN, no un detalle.** El
    sondeo corre como tarea independiente del bucle evaluador, así que puede
    pillar a `BotRunner._ejecutar` suspendido en un `await` del broker -justo
    después de mandar la orden de cierre y antes de registrarla. En esa
    ventana el exchange ya no reporta la posición, el sondeo la da por
    desaparecida y la cierra en la base... y luego el cierre normal, que
    seguía en vuelo, la cierra otra vez: **un fill duplicado**. Con el mismo
    `asyncio.Lock` que toma `paso_evaluador` alrededor de `on_tick`, los dos
    caminos nunca corren a la vez y el segundo encuentra la posición ya fuera
    de `bot.abiertas` (`_cerrar` la saca antes de soltar el cerrojo).

    Y el cerrojo se toma ANTES de pedir las posiciones, no después: si se
    tomara después, la lista podría envejecer mientras se espera al tick -y
    una posición abierta en ese tick, ausente de la foto anterior, se leería
    como "desaparecida del exchange" y se cerraría en la base recién abierta.
    Se paga por ello la latencia de una petición mientras el tick espera, que
    es del mismo orden que los `await` al broker que el tick ya hace con el
    cerrojo tomado, y está acotada por el `timeout` del cliente HTTP (20 s,
    ver `main()`): en el peor caso un tick sale tarde, nunca se queda
    esperando para siempre.

    No propaga, como el resto de los `paso_*`: si el exchange no responde, se
    registra y se reintenta en el próximo sondeo.
    """
    try:
        async with cerrojo:
            posiciones = posiciones_del_bot(await privado.get_posiciones())
            await bot.sondear_exchange(posiciones, ahora)
    except Exception as exc:  # noqa: BLE001
        log.warning("bot: sondeo del exchange fallido: %s", exc)


def marcar_ws_conectado(state, conectado: bool) -> None:
    """Callback de `BitgetWebsocket.run` (I1): mueve `state.ws_connected`.

    Función independiente (en vez de una closure anidada en `main()`, como
    antes) para poder probarla directamente con un `ScannerState` de
    prueba, sin construir un `Orchestrator` completo."""
    state.ws_connected = conectado


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parsea los argumentos de línea de comandos de `python -m scanner_volumen`.

    Función independiente (igual que `marcar_ws_conectado` y los `paso_*` de
    arriba, I-5) para poder probarla sin arrancar `main()` -que abre
    conexiones de red- ni el proceso completo.

    `--config` decide qué instancia es esta: separa el proceso de producción
    (VPS, `config.toml`) del de desarrollo (`config.dev.toml`), cada uno con
    su propio `db_path`, para que una corrida de prueba no pueda escribir
    físicamente en la base de datos de producción (ver `config.dev.toml`).
    El valor por defecto reproduce el comportamiento anterior a este cambio,
    así que la unidad systemd de producción sigue funcionando sin tocarla.
    """
    parser = argparse.ArgumentParser(
        prog="python -m scanner_volumen",
        description="Arranca el scanner de momentum de Bitget: sondea tickers, "
                     "evalúa señales y sirve el dashboard.",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config.toml"),
        help="ruta al fichero de configuración (por defecto: ./config.toml). "
             "Usa config.dev.toml para una instancia de desarrollo separada "
             "-con su propia base de datos y puerto- de la de producción.",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    cfg = load_config(args.config)
    # La doble llave del dinero real (Task 1, `bot/modo.py`), resuelta ANTES
    # que cualquier otra cosa -antes incluso de abrir la base de datos-: un
    # `ValueError` aquí tiene que impedir el arranque, no capturarse. Es
    # deliberado que no dependa de `bot.enabled`: si la configuración pide
    # `modo = "real"` y la segunda llave no está puesta, lo correcto es que el
    # proceso no arranque y se vea, no que arranque a medias porque el bot
    # venía apagado.
    modo = resolver_modo(cfg.bot, os.environ)
    # El modo EFECTIVO es el que se persiste (`bot_posiciones.modo`) y el que
    # ven el informe y el panel, así que viaja dentro de la propia `BotConfig`
    # que reciben el runner, la cartera y los frenos: `cfg.bot.modo` solo sabe
    # decir "real", y `real` y `real_lectura` son dos libros contables
    # distintos que no deben mezclarse. En `paper` esto es la misma
    # configuración de siempre, campo a campo.
    cfg_bot = replace(cfg.bot, modo=modo)
    # Procedencia (ver provenance.py): se resuelve UNA vez al arrancar, aquí
    # -no dentro de evaluate()/insert(), que corren en caliente miles de
    # veces- porque `code_revision` no cambia durante la vida del proceso
    # (I-6: mismo principio que "ahora" se ancla en el reloj del exchange en
    # vez de leerlo en cada sitio). Un proceso reiniciado tras un `git pull`
    # recalcula la revisión en su propio arranque.
    code_revision = get_code_revision(cwd=Path(__file__).resolve().parent.parent)
    log.info("code_revision=%s", code_revision)
    conn = open_db(Path(cfg.server.db_path))

    candle_repo = CandleRepo(conn)
    profile_repo = ProfileRepo(conn)
    signal_repo = SignalRepo(conn)
    state_transition_repo = StateTransitionRepo(conn)
    supply_repo = SupplyRepo(conn)
    maintenance_repo = MaintenanceRepo(conn)

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=20.0) as http:
        rest = BitgetRest(cfg.market.venue, cfg.rest.rate_limit_per_second, http)
        ws = BitgetWebsocket(cfg.market.venue)
        supply = SupplyCache(supply_repo, http, refresh_hours=cfg.supply.refresh_hours)
        bootstrapper = Bootstrapper(rest, candle_repo, profile_repo, cfg.profile)
        selector = UniverseSelector(cfg.universe)
        orq = Orchestrator(cfg, rest, ws, candle_repo, profile_repo,
                            signal_repo, state_transition_repo, supply, bootstrapper,
                            code_revision=code_revision)
        orq.state.stale_after_ms = int(cfg.dashboard.stale_after_seconds * 1000)
        tracker = OutcomeTracker(
            signal_repo, candle_repo, horizons=cfg.outcomes.horizons_minutes
        )

        bot = None
        piezas = None
        proveedor_saldo = None
        frenos = None  # se construye dentro del `if` de abajo, en todos los modos
        # Un único cerrojo para todo el bot: lo toman el tick del evaluador y
        # el paso de sondeo, que son los dos caminos que pueden cerrar una
        # posición. Ver `paso_sondeo` para el fill duplicado que esto evita.
        cerrojo_bot = asyncio.Lock()
        if cfg.bot.enabled:
            bot_repo = BotRepo(conn)
            # fija el capital la primera vez y respeta el ya guardado en
            # arranques posteriores: el saldo es un valor vivo, no se
            # reinicia en cada despliegue. Segmentado por modo (C): el día
            # que exista operativa `real`, no debe arrancar sobre el capital
            # del `paper`.
            bot_repo.set_equity_inicial(
                modo, bot_repo.equity_inicial(modo, defecto=cfg.bot.equity_inicial),
            )
            # igual patrón para `arrancado_ms`: sin él, la "Ventana" del
            # informe se deriva del primer trade en vez del arranque real, y
            # un bot que lleva días corriendo antes de operar por primera vez
            # publicaría una ventana mucho más corta de la real.
            bot_repo.set_arrancado_ms(bot_repo.arrancado_ms(defecto=ahora_ms()))
            params = StrategyParams()
            piezas = construir_piezas_del_bot(modo, params, cfg, http, os.environ)
            verificador = None
            if piezas.privado is not None:
                # UN SOLO proveedor de saldo para la cartera y para los
                # frenos: dos fuentes distintas hacen que la referencia y la
                # medida del freno de pérdida diaria diverjan (Task 11).
                proveedor_saldo = ProveedorSaldo(
                    piezas.privado,
                    edad_maxima_s=(cfg.bot.saldo_refresco_segundos
                                   * CICLOS_TOLERADOS_SIN_SALDO),
                )
                # Perezoso y por símbolo: en Bitget el apalancamiento no es
                # de la cuenta, así que aquí solo se construye; se consulta
                # antes de la primera entrada de cada par (Task 11).
                verificador = VerificadorCuenta(
                    params, piezas.privado.get_configuracion_symbol)
            # Los frenos se cablean en TODOS los modos -pero no los dos
            # frenos en todos (corrección de la ronda de revisión, que separó
            # lo que yo había juntado):
            #
            # - La PARADA DE EMERGENCIA vale en cualquier modo. Solo actúa
            #   cuando un humano crea el fichero, así que "cambiaría el
            #   comportamiento del proceso que mide la estrategia" no es un
            #   argumento en su contra: cambiarlo es exactamente lo que se le
            #   pide. Con el fichero ausente es inerte y `paper` sale
            #   idéntico. Dejarla sin cablear ahí era documentar en el manual
            #   de despliegue un control vivo que en el único modo que corre
            #   en producción no hacía nada.
            # - La PÉRDIDA DIARIA solo en los modos reales. Actúa sola, y en
            #   `paper` dejaría de abrir entradas que el backtest sí abre:
            #   rompería la comparación paper/backtest, que es para lo que
            #   existe ese modo. Donde no hay dinero real no tiene a quién
            #   proteger.
            #
            # `proveedor_saldo` es el MISMO objeto que recibe `LivePortfolio`
            # justo debajo (Task 11): la referencia del freno y la medida del
            # margen no pueden salir de fuentes distintas. En `real_lectura`
            # esa fuente es el saldo real de una cuenta que el bot simulado no
            # mueve, así que el freno solo saltará si el saldo baja por otra
            # vía (una operación manual, funding): mide lo que de verdad hay,
            # aunque en ese modo rara vez tenga nada que cortar.
            frenos = Frenos(cfg_bot, bot_repo, modo, proveedor_saldo,
                            perdida_diaria_activa=(modo != PAPER))
            bot = BotRunner(
                params, cfg_bot, bot_repo, piezas.broker,
                LivePortfolio(params, cfg_bot, bot_repo, proveedor_saldo),
                fill_de_cierre=(fill_de_cierre_no_disponible
                                if piezas.privado is not None else None),
                frenos=frenos, verificador=verificador,
            )
            log.info("bot ACTIVO en modo %s, equity %.2f",
                     modo, bot_repo.equity(modo))
        else:
            log.info("bot desactivado (bot.enabled = false)")

        if bot is not None:
            def _transiciones_de(symbol: str, desde: int):
                return [
                    TransitionRow(
                        ts=f["ts"], symbol=f["symbol"],
                        prev_state=State(f["prev_state"]),
                        new_state=State(f["new_state"]), price=f["price"],
                        direction=Direction(f["direction"]), score=f["score"],
                    )
                    for f in state_transition_repo.por_simbolo(symbol, desde)
                    if f["price"] is not None
                ]

            def _velas_de(symbol: str, desde: int):
                return [
                    CandleRow(ts=c.ts, open=c.open, high=c.high, low=c.low,
                              close=c.close)
                    for c in candle_repo.load(symbol, desde)
                ]

            ahora_arranque = orq.now_ms(ahora_ms())
            if modo == PAPER:
                # `paper`: la base de datos es la verdad, y recuperar lo que
                # quedó abierto es todo lo que hay que hacer. Exactamente
                # igual que antes de esta tarea. Se discrimina por MODO y no
                # por "¿hay proveedor de saldo?" a propósito: si un cambio
                # futuro dejara un modo real sin proveedor, esta rama lo
                # taparía en silencio -y arrancaría en real sin reconciliar-,
                # mientras que así revienta a la vista.
                await bot.reconstruir(_transiciones_de, _velas_de,
                                      _precio_de(orq), ahora_arranque)
            else:
                # La secuencia de arranque de los modos reales, en este orden
                # y no en otro:
                #
                # (1) Conseguir un saldo real ANTES de levantar ningún bucle.
                # No se captura: sin saldo no hay con qué dimensionar una
                # entrada ni contra qué medir el freno de pérdida diaria, y
                # arrancar igualmente significaría hacer ambas cosas sobre una
                # cifra inventada. Que el proceso no arranque es ruidoso y se
                # ve; un cero silencioso, no.
                saldo = await proveedor_saldo.refrescar()
                bot_repo.set_saldo_real(modo, saldo)
                log.info("bot: saldo real de la subcuenta: %.2f", saldo)
                # (2) La verificación de la configuración de la cuenta ya está
                # construida más arriba. No hay nada global que comprobar aquí:
                # es perezosa y por símbolo (`VerificadorCuenta`), y corre
                # antes de la primera entrada de cada par.
                #
                # (3) Reconciliar con el exchange, SOLO en `real`. En
                # `real_lectura` el broker es el de paper, así que las
                # posiciones de este libro (modo `real_lectura`, un libro
                # aparte del de `real`) son simuladas y NO existen en Bitget:
                # compararlas contra el exchange no diría "se cerraron
                # mientras estábamos caídos", diría "no están" para todas, y
                # además vetaría los símbolos de las posiciones reales que
                # hubiera abiertas -que no son suyas. En ese modo se
                # reconstruye como en paper, que es lo que su libro significa.
                if modo == REAL:
                    posiciones = posiciones_del_bot(
                        await piezas.privado.get_posiciones())
                    await bot.reconciliar_con_exchange(
                        posiciones, _transiciones_de, _velas_de,
                        _precio_de(orq), fill_de_cierre_no_disponible,
                        ahora_arranque)
                else:
                    await bot.reconstruir(_transiciones_de, _velas_de,
                                          _precio_de(orq), ahora_arranque)
                # (4) Fijar la referencia del día ANTES de la primera consulta
                # de `puede_abrir` (contrato escrito en los docstrings de
                # `Frenos`). Con el mismo proveedor inyectado en las dos
                # puntas el orden ya no puede producir el desajuste que motivó
                # ese contrato, pero respetarlo no cuesta nada y el día que
                # alguien quite el proveedor vuelve a ser lo único que separa
                # la referencia buena de la contable.
                frenos.registrar_saldo_del_dia(ahora_arranque, saldo)

        app = create_app(
            orq.state, signal_repo,
            bot_repo=(bot_repo if cfg.bot.enabled else None), modo=modo,
        )
        servidor = uvicorn.Server(
            uvicorn.Config(
                app, host=cfg.server.host, port=cfg.server.port, log_level="warning"
            )
        )

        async def bucle_tickers() -> None:
            """Refresca tickers y, con su propia cadencia, el universo.

            El cuerpo de cada iteración vive en `paso_tickers` (I-5), a
            nivel de módulo, para poder probarlo sin levantar `main()`
            entera; aquí solo se arma el `while True` con su cadencia
            (`ticker_poll_seconds`) y se hace persistir `ultimo_universo`
            entre iteraciones.
            """
            ultimo_universo = 0
            while True:
                ahora = orq.now_ms(ahora_ms())
                ultimo_universo = await paso_tickers(
                    orq, rest, selector, bootstrapper, supply,
                    ahora, ultimo_universo, cfg.universe.refresh_minutes,
                )
                await asyncio.sleep(cfg.engine.ticker_poll_seconds)

        async def bucle_evaluador() -> None:
            while True:
                ahora = orq.now_ms(ahora_ms())
                await paso_evaluador(orq, bootstrapper, ahora, bot, cerrojo_bot)
                await asyncio.sleep(cfg.engine.tick_seconds)

        async def bucle_outcomes() -> None:
            while True:
                await asyncio.sleep(cfg.outcomes.poll_seconds)
                await paso_outcomes(tracker, orq.now_ms(ahora_ms()))

        async def bucle_mantenimiento() -> None:
            """Poda diaria (I2) y recálculo diario del perfil de volumen (I4).

            `interval_hours` viene de `config.toml` (`[maintenance]`): todo
            umbral/cadencia de negocio vive ahí, no hardcodeado aquí. El
            cuerpo de cada paso -incluida la decisión de si toca correr y
            cuánto dormir- vive en `paso_mantenimiento` (I-5): aquí solo se
            arma el `while True` que encadena su resultado. A diferencia de
            los demás bucles, la espera no es una cadencia fija: es
            exactamente lo que `paso_mantenimiento` calcula que falta hasta
            el próximo vencimiento (o el respiro de reintento si el último
            intento fue un no-op), así que un mantenimiento vencido al
            arrancar corre ya en la primera vuelta en vez de esperar
            `interval_hours` completas.
            """
            while True:
                ahora = orq.now_ms(ahora_ms())
                espera_ms = await paso_mantenimiento(
                    orq, maintenance_repo, ahora, cfg.maintenance.interval_hours
                )
                await asyncio.sleep(espera_ms / 1000)

        async def bucle_saldo() -> None:
            """Refresca el saldo real de la subcuenta y lo persiste.

            Duerme ANTES del primer paso a propósito: el arranque ya obtuvo
            (y exigió) un saldo, así que volver a pedirlo de inmediato solo
            gastaría una petición."""
            while True:
                await asyncio.sleep(cfg.bot.saldo_refresco_segundos)
                await paso_saldo(proveedor_saldo, bot_repo, modo)

        async def bucle_sondeo() -> None:
            """Pregunta al exchange qué posiciones siguen vivas (Task 9).

            Duerme antes del primer paso por la misma razón que
            `bucle_saldo`: la reconciliación de arranque acaba de mirar
            exactamente eso, con más detalle."""
            while True:
                await asyncio.sleep(cfg.bot.sondeo_segundos)
                await paso_sondeo(bot, piezas.privado, cerrojo_bot,
                                  orq.now_ms(ahora_ms()))

        log.info(
            "config=%s dashboard en http://%s:%d",
            args.config, cfg.server.host, cfg.server.port,
        )
        tareas = [
            ws.run(
                orq.handle_ws_event,
                on_connection_change=lambda c: marcar_ws_conectado(orq.state, c),
            ),
            bucle_tickers(),
            bucle_evaluador(),
            bucle_outcomes(),
            bucle_mantenimiento(),
            servidor.serve(),
        ]
        if proveedor_saldo is not None:
            # los dos bucles que solo existen con un exchange de verdad
            # detrás; en `paper` no se arranca ninguno y el proceso corre
            # exactamente las mismas seis tareas de siempre.
            tareas.append(bucle_saldo())
        if bot is not None and modo == REAL:
            # el sondeo, solo en `real`: en `real_lectura` las posiciones
            # del bot son simuladas y "no están en el exchange" es su estado
            # normal, no una anomalía que investigar (ver la secuencia de
            # arranque, más arriba).
            tareas.append(bucle_sondeo())
        await asyncio.gather(*tareas)


if __name__ == "__main__":
    asyncio.run(main())
