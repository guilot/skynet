# scanner_volumen/app/orchestrator.py
"""Coordina fuentes de datos, motor y persistencia.

El evaluador corre a tick fijo y solo procesa símbolos marcados como sucios.
Eso desacopla la tasa de entrada (cientos de mensajes por segundo) de la de
cálculo, y hace que el comportamiento sea reproducible en tests inyectando
`now_ms` en lugar de leer el reloj.
"""
from __future__ import annotations

import asyncio
import logging

from scanner_volumen.app.bootstrap import plan_history_requests
from scanner_volumen.app.state import ScannerState, SymbolSnapshot
from scanner_volumen.bitget.rest import MAX_HISTORY_LIMIT
from scanner_volumen.bitget.ws import WsEvent
from scanner_volumen.config import Config
from scanner_volumen.engine.candles import CandleBuffer
from scanner_volumen.engine.metrics import MetricsBuilder
from scanner_volumen.engine.profile import VolumeProfile, build_profile, placeholder_profile
from scanner_volumen.models import State, Ticker
from scanner_volumen.provenance import UNKNOWN_REVISION, config_fingerprint
from scanner_volumen.scoring.score import score_symbol
from scanner_volumen.scoring.states import StateMachine, Transition
from scanner_volumen.storage.repos import CandleRepo, ProfileRepo, SignalRepo

MINUTO_MS = 60_000
DIA_MS = 1440 * MINUTO_MS

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self, cfg: Config, rest, ws, candle_repo: CandleRepo,
        profile_repo: ProfileRepo, signal_repo: SignalRepo, supply, bootstrapper,
        code_revision: str = UNKNOWN_REVISION,
    ) -> None:
        self.cfg = cfg
        self.rest = rest
        self.ws = ws
        self.candle_repo = candle_repo
        self.profile_repo = profile_repo
        self.signal_repo = signal_repo
        self.supply = supply
        self.bootstrapper = bootstrapper
        # Procedencia (ver `provenance.py`): el fingerprint se deriva de
        # `cfg` una sola vez aquí -es puro y determinista, no cambia durante
        # la vida del proceso- y se estampa en cada señal persistida más
        # abajo, en `evaluate`. `code_revision` la calcula el llamador
        # (`__main__.py`, que sí hace I/O de git a propósito, una sola vez al
        # arrancar) y se pasa ya resuelta; el valor por defecto
        # (`UNKNOWN_REVISION`) es solo para no obligar a cada test que
        # construye un Orchestrator a decidir una revisión que no le importa.
        self._config_fingerprint = config_fingerprint(cfg)
        self._code_revision = code_revision

        self.state = ScannerState()
        self.dirty: set[str] = set()
        self.buffers: dict[str, CandleBuffer] = {}
        self.profiles: dict[str, VolumeProfile] = {}
        self.tickers: dict[str, Ticker] = {}
        # símbolos que acaban de recibir un "snapshot" (reconexión de WS) y
        # todavía no han pasado por refill_gap; el bucle principal lo consume.
        self.reconnected: set[str] = set()
        # ts de la vela que era `current()` justo ANTES de aplicar un
        # snapshot de reconexión, indexado por símbolo. refill_gap mide el
        # hueco contra este valor en vez de contra buffer.current(), porque
        # para cuando corre ya se le aplicaron las velas del snapshot y
        # current() sería la más reciente (hueco ~= 0, C4a).
        self._reconnect_gap_from: dict[str, int] = {}
        # bootstraps reales lanzados en segundo plano por apply_universe,
        # indexados por símbolo; evita lanzar dos a la vez para el mismo
        # símbolo y mantiene una referencia viva para que la tarea no se
        # recolecte a mitad de vuelo.
        self._bootstrap_tasks: dict[str, asyncio.Task] = {}
        # rellenos de hueco de reinicio en caliente lanzados en segundo
        # plano por apply_universe (ver `_resolver_perfil`), indexados por
        # símbolo; registro separado de `_bootstrap_tasks` porque un
        # símbolo con perfil ya en disco nunca pasa por el bootstrap real
        # (Finding 2) y por tanto no debe contarse como "bootstrap en
        # vuelo" para el guard de dedup de `apply_universe`.
        self._gap_fill_tasks: dict[str, asyncio.Task] = {}
        # símbolos cuyo `self.profiles[simbolo]` es hoy un `placeholder_profile`
        # y no un perfil real; el dict `profiles` no distingue por sí solo
        # entre ambos, así que sin este registro un bootstrap fallido dejaría
        # al símbolo congelado en su placeholder para siempre (Finding 1: el
        # guard de dedup de `apply_universe` lo confundiría con un símbolo ya
        # resuelto).
        self._placeholder_symbols: set[str] = set()
        # símbolos excluidos del universo activo por libro fino: su
        # volumen típico de perfil (`VolumeProfile.typical_volume()`) está
        # por debajo de `cfg.universe.min_profile_median_volume` (ver
        # `_admite_libro`). El selector sigue trayéndolos en `ordered`
        # cada refresco de universo -no sabe nada de este rechazo, solo
        # aplica el prefiltro barato de volumen 24h-, así que sin esta
        # memoria `apply_universe` volvería a intentar resolver su perfil
        # (y, si no había disco, a rebootstrapear 14 días por REST) en
        # cada uno de esos refrescos. La única puerta de reingreso es
        # `_reevaluar_rechazados`, desde el mantenimiento diario.
        self._rejected_thin_book: set[str] = set()
        # símbolos cuyo perfil actual todavía NO ha pasado por `_admite_libro`
        # (arranque en frío: siguen con el placeholder que `apply_universe`
        # les asigna mientras su bootstrap real corre en segundo plano). El
        # filtro de libro fino solo puede aplicarse una vez existe un perfil
        # real -mientras tanto el símbolo ya se puntúa, rankea y muestra en el
        # dashboard con normalidad vía el fallback de mediana rolling, pero
        # nadie ha comprobado todavía si su libro es lo bastante líquido para
        # que ese score signifique algo. Sin esta memoria, un pump durante esa
        # ventana podía escalar a HOT y `evaluate` persistía la fila antes de
        # que el rechazo por libro fino llegara (medido en real: 56 filas en
        # 52 min de arranque en frío, la mayoría de libros de $0-800/min que
        # el filtro rechazó en cuanto llegó su perfil) -contaminando
        # precisamente el dataset que `signals` existe para dejar calibrar.
        # Se llena en `apply_universe` al asignar el placeholder y se vacía
        # dentro de `_admite_libro` en cuanto un perfil real (sea cual sea su
        # `confidence`) pasa por la puerta, aceptado o rechazado: baja
        # confianza (listing reciente) no es lo mismo que "sin validar", así
        # que un perfil real de baja confianza que ya pasó la puerta sale de
        # este set igual que uno de alta confianza. Nunca lo toca el reuso de
        # `_placeholder_symbols` en `_rellenar_hueco_en_fondo` -ese caso ya
        # tiene un perfil real y validado, solo pide que `apply_universe` lo
        # reconsidere por un hueco de histórico, no por el libro-.
        self._pending_book_validation: set[str] = set()

        self._metrics = MetricsBuilder(cfg.engine, cfg.profile)
        self._states = StateMachine(cfg.states)
        # ratchet del reloj del exchange (I6/C-1): último ts visto entre
        # tickers y velas del WS, nunca retrocede. Ver `now_ms`.
        self._clock_ms: int | None = None
        # umbral de negocio (severidad mínima que se persiste en `signals`),
        # desde config.toml: el TOML guarda el nombre del estado como texto
        # ("HOT"), aquí se convierte una vez al enum real.
        self._persisted_min_state = State(cfg.orchestrator.persisted_min_state)

    # --- entrada de datos ---

    def set_ticker(self, ticker: Ticker) -> None:
        self.tickers[ticker.symbol] = ticker

    # Tolerancia máxima entre un candidato a "ahora" (ticker o vela) y
    # `wall_clock_ms` antes de descartarlo como implausible (C-1): ni
    # `parse_tickers` ni `parse_candle` validan `ts`, y `_clock_ms` es
    # irrecuperable dentro de un proceso -el ratchet nunca retrocede- y
    # ahora alimenta `candle_repo.prune(now_ms - 14d)`, un borrado masivo.
    # Generoso a propósito: una caída real de REST/WS de varios días (ver
    # MAX_PAGINAS_DE_RELLENO, hasta ~66h) debe seguir aceptándose sin más
    # que un jitter razonable del reloj local; solo se busca cazar un ts
    # disparatado (bug de parseo, confusión s/ms, reloj del exchange roto).
    MAX_DERIVA_RELOJ_MS = 30 * DIA_MS

    def _newest_candle_floor(self) -> int | None:
        """Cota inferior de "ahora" derivada de las velas del WS (C-1).

        `poll_tickers` capta y registra cualquier fallo de REST y simplemente
        vuelve: si `/tickers` falla durante minutos mientras el WS sigue
        entregando velas, `now_ms` no debe congelarse en el último ticker
        bueno. La vela CERRADA más reciente de cualquier símbolo es una
        fuente independiente y continuamente disponible de "ahora" -el
        minuto en que cerró esa vela ya terminó, así que el instante actual
        es como mínimo `ts + 60_000`-. Se usa `closed(1)`, no `current()`,
        porque tras un reinicio en caliente `seed_buffer` siembra el buffer
        vía `backfill`, que deliberadamente nunca asigna `_current`
        (`CandleBuffer.backfill`): `closed(1)` sigue disponible en ese caso,
        `current()` no.
        """
        mejor: int | None = None
        for buffer in self.buffers.values():
            cerradas = buffer.closed(1)
            if not cerradas:
                continue
            candidato = cerradas[-1].ts + MINUTO_MS
            if mejor is None or candidato > mejor:
                mejor = candidato
        return mejor

    def now_ms(self, wall_clock_ms: int) -> int:
        """Fuente de "ahora" para todo el bucle principal (I6), salvo el
        propio arranque: el reloj del exchange, nunca el de pared.

        `models.Ticker.ts` es el timestamp que Bitget estampa en cada
        respuesta de `/tickers`; `poll_tickers` lo refresca cada
        `engine.ticker_poll_seconds`. Pero `/tickers` es una fuente que
        puede fallar (REST caído) sin que el WS se entere: por eso (C-1) el
        candidato también incluye `_newest_candle_floor()`, derivado de las
        velas que el WS sigue entregando de forma independiente. Se toma el
        máximo entre todos los símbolos conocidos y ambas fuentes -un solo
        ticker o vela desfasados no deben tirar del resto- y el resultado
        nunca retrocede (ratchet): `evaluate`, `refill_gap`, el
        mantenimiento diario y el resto del motor asumen `now_ms` monótono
        creciente (límite del día en curso, historial de RVOL, cooldown de
        la máquina de estados, cómputo de huecos, y ahora también
        `candle_repo.prune`), así que ni una corrección de reloj en el
        exchange ni una lectura puntual desfasada pueden mover el tiempo
        hacia atrás. Cualquier candidato que diste de `wall_clock_ms` más de
        `MAX_DERIVA_RELOJ_MS` se descarta como implausible antes de
        ratchetear (ver la constante).

        `wall_clock_ms` solo se usa como respaldo mientras `_clock_ms` sigue
        sin establecer -arranque en frío, antes de la primera respuesta de
        `/tickers` o vela de WS-: es el único momento en que el reloj de
        pared sigue siendo legítimo aquí, spec §13 ("nunca la hora local").
        En cuanto `_clock_ms` se establece, `wall_clock_ms` se ignora por
        completo para el "ahora" devuelto, incluso si diverge mucho del
        reloj del exchange -ese es justo el escenario de deriva que la spec
        identifica como riesgo-, salvo para el filtro de implausibilidad de
        arriba.

        Nota sobre "nunca retrocede": la garantía solo aplica una vez
        `_clock_ms` está establecido. Antes de eso, cada llamada devuelve
        `wall_clock_ms` directamente (sin ratchetear ese valor); si el reloj
        local va adelantado respecto al del exchange, el primer ticker o
        vela puede hacer que el valor devuelto dé un paso atrás justo en esa
        transición. Es un caso aceptado, no un bug: `wall_clock_ms` nunca es
        una fuente de verdad aquí (spec §13), así que no se ratchetea a
        través de él -hacerlo reintroduciría la propia deriva del reloj
        local que este método existe para ignorar-.
        """
        candidatos: list[int] = []
        for nombre, candidato in (
            ("ticker", max((t.ts for t in self.tickers.values()), default=None)),
            ("vela", self._newest_candle_floor()),
        ):
            if candidato is None:
                continue
            if abs(candidato - wall_clock_ms) > self.MAX_DERIVA_RELOJ_MS:
                log.warning(
                    "candidato a reloj de %s descartado por implausible: "
                    "%d (reloj local %d)", nombre, candidato, wall_clock_ms,
                )
                continue
            candidatos.append(candidato)

        candidato_final = max(candidatos, default=None)
        if candidato_final is not None and (
            self._clock_ms is None or candidato_final > self._clock_ms
        ):
            self._clock_ms = candidato_final
        if self._clock_ms is None:
            return wall_clock_ms
        return self._clock_ms

    async def _admite_libro(self, symbol: str, perfil: VolumeProfile) -> bool:
        """Puerta real del filtro de universo (dos etapas, ver
        `UniverseConfig`): decide si un perfil recién resuelto tiene
        actividad suficiente para que el símbolo se quede en el universo
        activo.

        Se llama justo después de resolver un perfil REAL (nunca sobre un
        placeholder, que siempre tiene `typical_volume() is None` por no
        tener slots) en los tres únicos puntos que producen uno:
        `apply_universe` (camino cálido, disco), `_bootstrap_en_fondo`
        (camino frío, REST) y el recálculo diario de `run_maintenance`. Si
        el libro es demasiado fino, deja al símbolo tan limpio como
        `apply_universe` deja a uno que el propio selector quitó del
        universo -desuscrito, sin buffer, sin perfil, sin estado- y lo
        recuerda en `_rejected_thin_book` para no reintentarlo en cada
        refresco (ver el comentario de ese atributo). Devuelve True si el
        símbolo puede seguir su camino normal (el llamador es quien asigna
        `self.profiles[symbol]`); False si ya se ha limpiado y el llamador
        debe saltárselo.

        `None` (perfil sin slots suficientes para ser representativo, C.f.
        `VolumeProfile.typical_volume`) NO se trata como fino: un símbolo
        recién bootstrapeado con muy poco histórico real todavía no tiene
        forma de demostrar que es líquido, y negarle la entrada por falta
        de datos sería indistinguible de penalizar el arranque en frío que
        el propio placeholder existe para no penalizar.

        Este es también el único punto que saca a un símbolo de
        `_pending_book_validation`: en cuanto esta función decide -acepte o
        rechace- el símbolo deja de estar "sin validar" para `evaluate`, que
        usa ese set para decidir si puede persistir en `signals` (ver su
        comentario en `__init__`).
        """
        tipico = perfil.typical_volume()
        self._pending_book_validation.discard(symbol)
        if tipico is None or tipico >= self.cfg.universe.min_profile_median_volume:
            return True

        log.warning(
            "%s excluido del universo activo: volumen típico del perfil "
            "%.2f USDT/min por debajo del mínimo %.2f",
            symbol, tipico, self.cfg.universe.min_profile_median_volume,
        )
        self._rejected_thin_book.add(symbol)
        self.buffers.pop(symbol, None)
        self.profiles.pop(symbol, None)
        self.tickers.pop(symbol, None)
        self._placeholder_symbols.discard(symbol)
        self._reconnect_gap_from.pop(symbol, None)
        self.state.drop(symbol)
        self._metrics.forget(symbol)
        self._states.forget(symbol)
        if self.ws is not None:
            await self.ws.unsubscribe([symbol])
        return False

    async def _reevaluar_rechazados(self, now_ms: int) -> None:
        """Única puerta de reingreso para un símbolo rechazado por libro
        fino (I: "la puerta no puede ser permanente").

        Se llama desde `run_maintenance` (cadencia diaria,
        `cfg.maintenance.interval_hours`): al estar desuscrito del WS desde
        el rechazo, un símbolo en `_rejected_thin_book` no recibe ninguna
        vela nueva, así que su registro en `candle_repo` solo encoge con
        cada poda diaria (I2) y nunca podría reflejar una mejora real de
        su libro -por eso este reingreso NO reutiliza
        `_recalcular_perfil_de_mantenimiento` (que lee de `candle_repo`)
        como hace el resto de `run_maintenance` con los símbolos activos,
        sino que pide historial fresco de verdad vía
        `self.bootstrapper.bootstrap_symbol`, igual que un símbolo
        genuinamente nuevo. Se hace una vez al día, no en cada refresco de
        universo (15 min): un libro que sigue muerto no cambia en 15
        minutos, y comprobar cada símbolo rechazado a esa cadencia con
        ~cientos de páginas REST cada uno saturaría la tasa de peticiones
        para nada.
        """
        for symbol in list(self._rejected_thin_book):
            try:
                perfil = await self.bootstrapper.bootstrap_symbol(symbol, now_ms)
            except Exception as exc:  # noqa: BLE001 - un reintento fallido no debe tumbar el mantenimiento
                log.warning("reevaluación de rechazo fallida para %s: %s", symbol, exc)
                continue

            tipico = perfil.typical_volume()
            if tipico is None or tipico < self.cfg.universe.min_profile_median_volume:
                continue  # sigue fino (o sin datos suficientes): se queda fuera

            self._rejected_thin_book.discard(symbol)
            self.profiles[symbol] = perfil
            await self.seed_buffer(symbol, now_ms)
            self.dirty.add(symbol)
            if self.ws is not None:
                await self.ws.subscribe([symbol])
            log.info(
                "%s vuelve al universo activo: volumen típico del perfil %.2f USDT/min",
                symbol, tipico,
            )

    async def ensure_profile(self, symbol: str, now_ms: int) -> VolumeProfile:
        if symbol in self.profiles:
            return self.profiles[symbol]
        perfil = await self._load_or_bootstrap(symbol, now_ms)
        self.profiles[symbol] = perfil
        await self.seed_buffer(symbol, now_ms)
        return perfil

    async def _load_or_bootstrap(self, symbol: str, now_ms: int) -> VolumeProfile:
        """Perfil real: de disco si ya existe, o descargado por completo si no.

        Bloquea hasta tener el perfil definitivo; existe separado de
        `ensure_profile` para que `_bootstrap_en_fondo` pueda lanzarlo en una
        tarea de fondo sin esperar a que termine (C1) mientras
        `ensure_profile` sigue siendo síncrono para quien lo llama
        directamente. El caso de perfil ya en disco cubre también el hueco
        de reinicio en caliente de forma síncrona (`hueco_en_fondo=False`):
        quien llama aquí -`ensure_profile` o el bootstrap de fondo- ya
        espera a un resultado definitivo, así que no gana nada difiriendo
        el relleno.
        """
        perfil = await self._resolver_perfil(symbol, now_ms, hueco_en_fondo=False)
        if perfil is not None:
            return perfil
        return await self.bootstrapper.bootstrap_symbol(symbol, now_ms)

    async def _resolver_perfil(
        self, symbol: str, now_ms: int, *, hueco_en_fondo: bool
    ) -> VolumeProfile | None:
        """Único punto del código que resuelve un "reinicio en caliente".

        Carga el perfil de disco si ya existe; si lo hay, lo marca cargado
        en el bootstrapper y dispara el relleno del hueco de histórico
        entre la parada y el reinicio (C4c: sin esto la spec y el README
        prometen algo que el código no cumplía). Devuelve `None` si no hay
        nada en disco, para que el llamador sepa que hace falta un
        bootstrap real por REST.

        `_load_or_bootstrap` (usado por `ensure_profile` y por el bootstrap
        de fondo) y `apply_universe` compartían antes cada uno su propia
        copia de este "si hay perfil en disco..."; con dos copias
        divergentes, solo una -la de `_load_or_bootstrap`, inalcanzable en
        producción porque `apply_universe` nunca la invoca- rellenaba el
        hueco. `hueco_en_fondo` es la única diferencia legítima entre los
        dos llamadores: `apply_universe` no puede permitirse esperar a que
        termine un relleno por REST antes de seguir con el resto del
        universo (mismo motivo que el bootstrap frío, C1), así que pide el
        relleno en una tarea de fondo separada (`_gap_fill_tasks`);
        `ensure_profile`, en cambio, es una llamada directa que sí espera un
        perfil "terminado".
        """
        perfil = self.profile_repo.load(symbol)
        if perfil is None:
            return None
        self.bootstrapper.mark_loaded(symbol)
        if hueco_en_fondo:
            self._lanzar_relleno_de_hueco_en_fondo(symbol, now_ms)
        else:
            await self._rellenar_hueco_de_reinicio(symbol, now_ms)
        return perfil

    def _lanzar_relleno_de_hueco_en_fondo(self, symbol: str, now_ms: int) -> None:
        """Lanza `_rellenar_hueco_de_reinicio` sin esperarlo (usado por
        `apply_universe`, ver `_resolver_perfil`).

        Sin `self.rest` el relleno no hace nada (mismo guard que
        `_rellenar_hueco_de_reinicio`): lanzar la tarea igualmente solo
        dejaría una `Task` colgando sin ningún trabajo real que hacer, así
        que se corta aquí. El dedup por símbolo evita lanzar dos rellenos a
        la vez si `apply_universe` viera el mismo símbolo dos veces antes de
        que el primero termine.
        """
        if self.rest is None or symbol in self._gap_fill_tasks:
            return
        tarea = asyncio.create_task(self._rellenar_hueco_en_fondo(symbol, now_ms))
        self._gap_fill_tasks[symbol] = tarea

    async def _rellenar_hueco_en_fondo(self, symbol: str, now_ms: int) -> None:
        """Cubre el hueco por REST y vuelve a sembrar el buffer con lo que
        quedó guardado en SQLite.

        `apply_universe` ya sembró el buffer con lo que había en disco ANTES
        de que este relleno terminase (por eso corre en segundo plano, C1);
        sin este segundo `seed_buffer`, las velas del hueco quedarían en
        SQLite pero nunca llegarían al buffer, y `session_volume`/`vwap`
        seguirían calculándose con el hueco sin cubrir hasta el siguiente
        reinicio.
        """
        try:
            await self._rellenar_hueco_de_reinicio(symbol, now_ms)
            if symbol in self._rejected_thin_book:
                # el símbolo fue excluido por libro fino (`_admite_libro`)
                # mientras este relleno corría en segundo plano -lanzado
                # por `_resolver_perfil` antes de que `apply_universe`
                # tuviera ocasión de rechazarlo (Finding libro fino, C1):
                # sembrar el buffer aquí resucitaría un símbolo que ya se
                # dejó sin buffer a propósito.
                return
            await self.seed_buffer(symbol, now_ms)
            self.dirty.add(symbol)
        except Exception as exc:  # noqa: BLE001 - un relleno de fondo fallido no debe tumbar el bucle
            # a diferencia de `_bootstrap_en_fondo`, aquí NO hay placeholder:
            # `self.profiles[symbol]` ya es un perfil real (el camino cálido
            # lo asigna síncronamente en `apply_universe` antes de lanzar esta
            # tarea), así que sin marcar algo el guard de dedup de
            # `apply_universe` ("ya tiene un perfil real: nada que hacer")
            # daría al símbolo por resuelto para siempre y el hueco (mismo
            # fallo silencioso-de-métricas de Finding 1, por otra puerta)
            # nunca se reintentaría. Se reutiliza `_placeholder_symbols` como
            # el marcador general de "este símbolo necesita que
            # apply_universe lo vuelva a resolver" -su propio docstring en
            # `apply_universe` ya documenta que ese set puede convivir con
            # perfiles reales en `self.profiles`, no solo con placeholders-
            # aunque el perfil de este símbolo nunca deje de ser real.
            log.warning("relleno de hueco en segundo plano fallido para %s: %s", symbol, exc)
            self._placeholder_symbols.add(symbol)
        finally:
            self._gap_fill_tasks.pop(symbol, None)

    async def _rellenar_hueco_de_reinicio(self, symbol: str, now_ms: int) -> None:
        """Cubre en SQLite el hueco de histórico de un símbolo cuyo perfil
        ya vivía en disco (reinicio en caliente).

        `bootstrap_symbol` es hoy el único llamador de `plan_history_requests`
        aparte de este método, y antes solo se invocaba cuando NO había
        perfil guardado: con perfil en disco el hueco entre la parada y el
        reinicio nunca se rellenaba (C4c), pese a que la spec y el README
        prometen que "los reinicios posteriores solo rellenan el hueco".
        Usa el mismo `plan_history_requests` que `bootstrap_symbol` -sin
        reconstruir el perfil, que ya es válido y no hace falta recalcular
        aquí-. No toca el buffer directamente: quien llama a este método
        (`_resolver_perfil`, directa o vía `_rellenar_hueco_en_fondo`)
        siempre hace un `seed_buffer` después, que relee de SQLite y recoge
        estas velas recién guardadas.
        """
        if self.rest is None:
            return
        paginas = plan_history_requests(
            self.candle_repo.latest_ts(symbol), now_ms, self.cfg.profile.history_days
        )
        for end_time in paginas:
            try:
                velas = await self.rest.get_history_candles(symbol, end_time_ms=end_time)
            except Exception as exc:  # noqa: BLE001 - una página perdida no aborta el relleno
                log.warning(
                    "página de relleno (reinicio en caliente) fallida para %s en %d: %s",
                    symbol, end_time, exc,
                )
                continue
            if velas:
                self.candle_repo.save_many(symbol, velas)

    async def seed_buffer(self, symbol: str, now_ms: int) -> None:
        """Siembra el buffer con el histórico ya persistido en SQLite.

        Sin esto, `Orchestrator.buffers` solo se rellena con lo que llega por
        WS desde que arrancó el proceso: `session_volume` y `vwap` medirían
        "desde que arrancó el proceso" en vez de "desde las 00:00 UTC" (C3),
        aunque el bootstrap ya hubiese guardado semanas de velas en disco.
        Carga desde el inicio del día en curso, o desde `now_ms - capacity`
        si eso cae más atrás (para no pedir más de lo que el buffer puede
        retener), lo que sea anterior.
        """
        buffer = self.buffers.setdefault(symbol, CandleBuffer(symbol))
        inicio_dia = (now_ms // DIA_MS) * DIA_MS
        desde = min(inicio_dia, now_ms - buffer.capacity * MINUTO_MS)
        historicas = self.candle_repo.load(symbol, desde)
        if historicas:
            buffer.backfill(historicas)

    async def handle_ws_event(self, event: WsEvent) -> None:
        if event.kind not in ("snapshot", "update") or not event.symbol:
            return
        buffer = self.buffers.setdefault(event.symbol, CandleBuffer(event.symbol))
        if event.kind == "snapshot":
            # capturamos el estado del buffer ANTES de aplicar las velas del
            # snapshot: si lo hiciéramos después, `current()` ya sería la
            # vela más reciente que el propio snapshot acaba de traer, y
            # refill_gap mediría un hueco ~= 0 (C4a).
            previa = buffer.current()
            if previa is None:
                # reinicio en caliente: `seed_buffer` siembra el buffer vía
                # `backfill`, que deliberadamente nunca asigna `_current`
                # (ninguna de esas velas está "en curso" de verdad, ver
                # `CandleBuffer.backfill`). Sin este fallback, `current()`
                # siendo None se confundiría con un símbolo nuevo sin
                # historia, y el primer snapshot tras el reinicio perdería
                # el hueco real (refill_gap acabaría comparando contra la
                # propia vela que el snapshot trae, hueco ~= 0).
                cerradas_previas = buffer.closed(1)
                previa = cerradas_previas[-1] if cerradas_previas else None
            if previa is not None:
                # `setdefault`, no asignación directa: si llegan dos
                # snapshots antes de que el bucle principal drene
                # `reconnected` y llame a refill_gap, el segundo snapshot ya
                # ve como `current()` la vela que trajo el primero, así que
                # sobrescribir aquí acortaría el hueco real al tramo entre
                # ambos snapshots en vez de medirlo desde la última vela de
                # antes de la caída.
                self._reconnect_gap_from.setdefault(event.symbol, previa.ts)
            else:
                # símbolo genuinamente nuevo (sin vela en curso ni historia
                # seedeada): no hay hueco real que medir, se limpia
                # cualquier entrada vieja que pudiera quedar de un ciclo
                # anterior del símbolo.
                self._reconnect_gap_from.pop(event.symbol, None)
        cerradas = []
        for vela in event.candles:
            if buffer.upsert(vela):
                anterior = buffer.closed(1)
                if anterior:
                    cerradas.extend(anterior)
        if cerradas:
            self.candle_repo.save_many(event.symbol, cerradas)
        self.dirty.add(event.symbol)
        if event.kind == "snapshot":
            self.reconnected.add(event.symbol)

    async def poll_tickers(self, now_ms: int) -> None:
        """Refresca los tickers por REST y marca sucios los símbolos afectados.

        El WS solo trae velas; ret_24h, volumen 24h, funding y open interest
        llegan exclusivamente por este camino, así que un símbolo puede
        necesitar reevaluación aunque no haya cerrado ninguna vela nueva.
        Un fallo de REST no debe tumbar el bucle: se registra y se reintenta
        en el siguiente poll.
        """
        if self.rest is None:
            return
        try:
            tickers = await self.rest.get_tickers()
        except Exception as exc:  # noqa: BLE001 - un poll fallido no tumba el bucle
            log.warning("poll_tickers fallido: %s", exc)
            return
        for ticker in tickers:
            if ticker.symbol not in self.buffers:
                continue
            self.set_ticker(ticker)
            self.dirty.add(ticker.symbol)

    # --- evaluación ---

    @staticmethod
    def _ultima_vela_ts(buffer: CandleBuffer, respaldo: int) -> int:
        """Ts de la vela más reciente conocida del buffer (I-2b).

        `SymbolSnapshot.updated_ms` usaba `now_ms` -el instante de
        evaluación-, pero `poll_tickers` marca sucio cada símbolo con
        buffer cada `ticker_poll_seconds` sin importar si el WS sigue vivo,
        así que `evaluate` seguía reescribiendo `updated_ms` aunque el WS
        llevara minutos muerto: el marcador de obsolescencia del dashboard
        (`now_ms - updated_ms > stale_after_ms`) nunca disparaba en el
        escenario exacto para el que existe. Se ancla en cambio al ts de la
        última vela real -la del WS, la única fuente de velas-, que deja de
        avanzar en cuanto el WS deja de entregar.

        Prioriza la vela en curso (más fresca); si no hay (p. ej. justo tras
        un reinicio en caliente, antes del primer evento de WS: `seed_buffer`
        siembra vía `backfill`, que deliberadamente nunca asigna `_current`),
        cae a la última cerrada. `respaldo` (el propio `now_ms`) solo se usa
        si el buffer no tiene ninguna vela todavía -no debería ocurrir en la
        práctica, `evaluate` ya exige `buffer is not None`, pero es más
        honesto que devolver un `None`/0 que el dashboard confundiría con
        "obsoleto desde siempre"-.
        """
        actual = buffer.current()
        if actual is not None:
            return actual.ts
        cerradas = buffer.closed(1)
        if cerradas:
            return cerradas[-1].ts
        return respaldo

    def evaluate(self, now_ms: int) -> list[Transition]:
        transiciones: list[Transition] = []
        pendientes, self.dirty = self.dirty, set()

        for simbolo in pendientes:
            buffer = self.buffers.get(simbolo)
            perfil = self.profiles.get(simbolo)
            if buffer is None or perfil is None:
                continue

            metricas = self._metrics.compute(
                simbolo, buffer, perfil, self.tickers.get(simbolo),
                self.supply.market_cap(simbolo), now_ms,
            )

            desglose = score_symbol(metricas, self.cfg.score)
            transicion = self._states.update(simbolo, desglose.total, now_ms)

            self.state.put(
                SymbolSnapshot(
                    symbol=simbolo, metrics=metricas, breakdown=desglose,
                    state=self._states.state_of(simbolo),
                    updated_ms=self._ultima_vela_ts(buffer, now_ms),
                )
            )

            if transicion is not None:
                transiciones.append(transicion)
                if (
                    transicion.escalated
                    and transicion.current.rank >= self._persisted_min_state.rank
                    # el libro fino solo puede evaluarse una vez existe un
                    # perfil real (`_admite_libro`); mientras el símbolo siga
                    # con el placeholder de arranque en frío, se sigue
                    # puntuando/rankeando/mostrando con normalidad, pero no
                    # se escribe en `signals` -ver el comentario de
                    # `_pending_book_validation` en `__init__`.
                    and simbolo not in self._pending_book_validation
                ):
                    self.signal_repo.insert(
                        metricas, desglose, transicion.current,
                        self._config_fingerprint, self._code_revision,
                    )

        return transiciones

    # --- relleno de huecos tras reconexión ---

    # Tope de páginas de 200 velas que un solo relleno pedirá por REST. 20
    # páginas x 200 min = 4000 min (~66 h) de hueco cubierto: una caída de
    # WS de casi 3 días es ya un escenario extremo, y sin este tope un reloj
    # atascado o un `now_ms` corrupto encadenaría peticiones sin fin.
    MAX_PAGINAS_DE_RELLENO = 20

    async def refill_gap(self, symbol: str, now_ms: int) -> None:
        """Rellena por REST las velas perdidas durante una desconexión.

        El snapshot que envía el WebSocket al reconectar cubre solo las últimas
        velas; una caída larga deja un hueco que falsearía el VWAP de sesión y
        el RVOL acumulado. `get_candles` no acepta `endTime`, así que para
        cubrir huecos largos hay que paginar hacia atrás con
        `get_history_candles`, igual que hace el bootstrap inicial.
        """
        buffer = self.buffers.get(symbol)
        if buffer is None or self.rest is None:
            # se limpia igualmente: dejar la entrada viva aquí sería un
            # leak silencioso -nunca se popea si no se llega a esta línea-
            # que además envenenaría un refill_gap posterior si `rest` o
            # `buffer` se recuperan más tarde para el mismo símbolo, con un
            # `anterior_ts` de una reconexión ya vieja.
            self._reconnect_gap_from.pop(symbol, None)
            return
        # el hueco se mide contra la última vela que teníamos ANTES del
        # snapshot de reconexión (capturada en handle_ws_event), no contra
        # buffer.current() ya refrescado por el propio snapshot (C4a). Si no
        # hay una entrada capturada (p. ej. refill_gap se llama fuera del
        # flujo de reconexión), se cae a la vela en curso y, si tampoco la
        # hay (reinicio en caliente antes del primer WS), a la última vela
        # cerrada ya seedeada -mismo fallback que handle_ws_event.
        anterior_ts = self._reconnect_gap_from.pop(symbol, None)
        if anterior_ts is None:
            actual = buffer.current()
            if actual is not None:
                anterior_ts = actual.ts
            else:
                cerradas = buffer.closed(1)
                if not cerradas:
                    return
                anterior_ts = cerradas[-1].ts
        hueco_min = (now_ms - anterior_ts) // 60_000
        if hueco_min <= self.cfg.orchestrator.gap_tolerance_minutes:
            return

        # Misma convención que plan_history_requests: la última vela guardada
        # ya cubre su propio minuto, así que el hueco empieza en el siguiente.
        desde = anterior_ts + MINUTO_MS
        minutos = max(0, (now_ms - desde) // MINUTO_MS)
        # Se verificó contra la API real que `endTime` es exclusivo (una
        # petición con endTime a las 20:08 devolvió velas hasta las 20:07), así
        # que en teoría `minutos` páginas ya cubrirían el tramo [desde, now_ms].
        # Pero este método existe justo para que un hueco silencioso no
        # falsee el VWAP y el RVOL de sesión, así que no queremos que su
        # corrección dependa de un detalle de la API que no podemos
        # reverificar desde los tests: se pide una página de más (+1) a
        # propósito, como seguro, para que la cuenta salga sea cual sea la
        # semántica real de `endTime`. La redundancia sale gratis porque
        # CandleRepo.save_many hace upsert.
        paginas_necesarias = -(-(minutos + 1) // MAX_HISTORY_LIMIT)  # división hacia arriba
        paginas_a_pedir = min(paginas_necesarias, self.MAX_PAGINAS_DE_RELLENO)

        if paginas_necesarias > self.MAX_PAGINAS_DE_RELLENO:
            cubierto_desde_ms = (
                now_ms - self.MAX_PAGINAS_DE_RELLENO * MAX_HISTORY_LIMIT * MINUTO_MS
            )
            log.warning(
                "relleno parcial para %s: el hueco de %d min necesita %d páginas "
                "y el tope es %d; queda sin cubrir el tramo %d-%d",
                symbol, hueco_min, paginas_necesarias, self.MAX_PAGINAS_DE_RELLENO,
                desde, cubierto_desde_ms,
            )

        recibidas = []
        for i in range(paginas_a_pedir):
            end_time = now_ms - i * MAX_HISTORY_LIMIT * MINUTO_MS
            try:
                velas = await self.rest.get_history_candles(symbol, end_time_ms=end_time)
            except Exception as exc:  # noqa: BLE001 - una página perdida no aborta el relleno
                log.warning("página de relleno fallida para %s en %d: %s", symbol, end_time, exc)
                continue
            recibidas.extend(velas)

        if not recibidas:
            return

        # `backfill`, no `upsert`: las velas recibidas son anteriores a la
        # vela en curso (que ya llegó por el snapshot), y `upsert` las
        # descartaría en silencio por ser "más viejas que la actual" -ese es
        # justo su propósito normal, evitar que un mensaje de WS tardío
        # reabra una vela ya cerrada- pero aquí es contraproducente (C4b).
        buffer.backfill(recibidas)
        self.candle_repo.save_many(symbol, recibidas)
        self.dirty.add(symbol)

    # --- ciclo de vida ---

    async def apply_universe(self, update, now_ms: int) -> None:
        """Aplica un cambio de universo sin bloquear en el bootstrap (C1).

        En frío, descargar 14 días de histórico son ~101 páginas REST por
        símbolo tras un token bucket de 10 req/s: esperar a `ensure_profile`
        de cada símbolo antes de suscribir el WS dejaría el escáner ~25 min
        sin una sola vela ni un ticker. Por eso el WS se suscribe primero, y
        el bootstrap real de cada símbolo se lanza como tarea de fondo,
        sembrando antes un perfil provisional de baja confianza para que el
        símbolo puntúe desde el primer minuto vía el fallback de mediana
        rolling en vez de quedarse sin puntuar hasta que termine su descarga.

        Dos matices sobre ese placeholder:
        - Un símbolo cuyo bootstrap de fondo falló se reintenta en el
          siguiente `apply_universe` que aún lo traiga en `ordered`, en vez
          de quedarse congelado en el placeholder para siempre: el guard de
          dedup no mira solo `self.profiles` (que mezcla placeholders y
          perfiles reales) sino `self._placeholder_symbols`.
        - Si el perfil ya existe en disco (arranque en caliente), se carga
          de forma síncrona aquí mismo y se usa directamente: ni placeholder
          ni tarea de fondo de bootstrap, porque esa maquinaria existe para
          el bootstrap lento por REST, no para una lectura local de SQLite.
          Pero el reinicio en caliente puede tener igualmente un hueco de
          histórico que tapar (C4c, ver `_resolver_perfil`), y taparlo por
          REST sí puede ser lento: por eso ese relleno concreto SÍ se lanza
          en su propia tarea de fondo (`_gap_fill_tasks`), separada de
          `_bootstrap_tasks` para no confundir al guard de dedup de más
          arriba con un bootstrap real en vuelo.
        """
        for simbolo in update.removed:
            self.buffers.pop(simbolo, None)
            self.profiles.pop(simbolo, None)
            self.tickers.pop(simbolo, None)
            self._placeholder_symbols.discard(simbolo)
            self._reconnect_gap_from.pop(simbolo, None)
            self.state.drop(simbolo)
            self._metrics.forget(simbolo)
            # decisión deliberada, no accidental: se descarta también la
            # histéresis/cooldown de alerta del símbolo (ver el docstring de
            # `StateMachine.forget`), por la misma clase de crecimiento sin
            # límite que motivó `_metrics.forget` -y para que un reingreso
            # sea "nuevo" en todos los sentidos, igual que ya lo es sin
            # buffer, sin perfil y sin historial de RVOL.
            self._states.forget(simbolo)
            # un símbolo que el propio selector saca del universo (volumen
            # 24h por debajo del prefiltro, tras agotar la gracia) ya no
            # tiene sentido seguir recordándolo como "rechazado por libro
            # fino": ni siquiera va a volver a aparecer en `ordered`, así
            # que dejarlo en `_rejected_thin_book` sería una fuga lenta
            # (mismo espíritu que el resto de la limpieza de este bucle).
            self._rejected_thin_book.discard(simbolo)
            # mismo espíritu: un símbolo que sale del universo ya no puede
            # "validarse" nunca (no va a recibir más velas ni bootstrap), así
            # que dejarlo aquí sería otra fuga lenta.
            self._pending_book_validation.discard(simbolo)
            tarea = self._bootstrap_tasks.pop(simbolo, None)
            if tarea is not None:
                tarea.cancel()
            tarea_hueco = self._gap_fill_tasks.pop(simbolo, None)
            if tarea_hueco is not None:
                tarea_hueco.cancel()

        if self.ws is not None:
            if update.added:
                await self.ws.subscribe(sorted(update.added))
            if update.removed:
                await self.ws.unsubscribe(sorted(update.removed))

        for simbolo in update.ordered:
            if simbolo in self._rejected_thin_book:
                # libro fino ya conocido (ver `_admite_libro`): no se
                # reintenta en cada refresco de universo, solo en el
                # mantenimiento diario (`_reevaluar_rechazados`).
                continue
            if simbolo in self._bootstrap_tasks:
                continue  # ya hay un bootstrap en vuelo para este símbolo
            # Nota: este guard NO mira `_gap_fill_tasks` -a propósito, pero es
            # un acoplamiento no obvio y por eso se deja explícito aquí. Hoy
            # es seguro porque el camino cálido asigna `self.profiles[simbolo]`
            # de forma SÍNCRONA (línea más abajo) antes de lanzar el relleno
            # de fondo, así que la siguiente pasada de `apply_universe` ya lo
            # atrapa en el guard de perfil justo debajo, sin necesitar mirar
            # `_gap_fill_tasks` para evitar un segundo relleno concurrente
            # (`_lanzar_relleno_de_hueco_en_fondo` tiene su propio dedup para
            # eso). Si el camino cálido dejara alguna vez de asignar el
            # perfil de forma síncrona, este guard tendría que empezar a
            # mirar `_gap_fill_tasks` también.
            if simbolo in self.profiles and simbolo not in self._placeholder_symbols:
                continue  # ya tiene un perfil real: nada que hacer (Finding 1)

            # lectura síncrona barata en disco primero (Finding 2): un
            # arranque en caliente no debe pasar por placeholder + tarea de
            # fondo de bootstrap, esa maquinaria existe solo para el
            # bootstrap lento por REST, no para un SELECT local. El relleno
            # del hueco de reinicio sí puede ser lento, así que
            # `_resolver_perfil` lo lanza en segundo plano
            # (`hueco_en_fondo=True`) en vez de esperarlo aquí.
            perfil_de_disco = await self._resolver_perfil(
                simbolo, now_ms, hueco_en_fondo=True
            )
            if perfil_de_disco is not None:
                if not await self._admite_libro(simbolo, perfil_de_disco):
                    continue  # libro fino: _admite_libro ya limpió el símbolo
                self.profiles[simbolo] = perfil_de_disco
                self._placeholder_symbols.discard(simbolo)
                await self.seed_buffer(simbolo, now_ms)
                self.dirty.add(simbolo)
                continue

            self.profiles[simbolo] = placeholder_profile(simbolo)
            self._placeholder_symbols.add(simbolo)
            # el placeholder nunca ha pasado por `_admite_libro` (siempre
            # tiene `typical_volume() is None`, ver su docstring): hasta que
            # el bootstrap real termine y lo valide, `evaluate` no debe
            # persistir señales para este símbolo aunque puntúe HOT o más.
            self._pending_book_validation.add(simbolo)
            await self.seed_buffer(simbolo, now_ms)
            tarea = asyncio.create_task(self._bootstrap_en_fondo(simbolo, now_ms))
            self._bootstrap_tasks[simbolo] = tarea

    async def _bootstrap_en_fondo(self, symbol: str, now_ms: int) -> None:
        """Obtiene el perfil real y sustituye el provisional cuando termina.

        Fire-and-forget desde `apply_universe`: un fallo aquí no debe tumbar
        el bucle principal, así que se registra y se abandona ese símbolo con
        el perfil provisional hasta el siguiente refresco de universo.
        """
        try:
            perfil = await self._load_or_bootstrap(symbol, now_ms)
            if not await self._admite_libro(symbol, perfil):
                return  # libro fino: _admite_libro ya limpió el símbolo
            self.profiles[symbol] = perfil
            self._placeholder_symbols.discard(symbol)
            await self.seed_buffer(symbol, now_ms)
            self.dirty.add(symbol)
        except Exception as exc:  # noqa: BLE001 - un bootstrap de fondo fallido no tumba el bucle
            # el símbolo queda marcado en self._placeholder_symbols (nunca se
            # tocó aquí), así que el siguiente apply_universe con este símbolo
            # todavía en `ordered` reintentará el bootstrap en vez de dejarlo
            # congelado en el placeholder para siempre (Finding 1).
            log.warning("bootstrap en segundo plano fallido para %s: %s", symbol, exc)
        finally:
            self._bootstrap_tasks.pop(symbol, None)

    # --- mantenimiento diario ---

    async def run_maintenance(self, now_ms: int) -> None:
        """Tarea de mantenimiento diaria: poda velas fuera de la ventana
        retenida (I2), recalcula el perfil de volumen de cada símbolo con
        perfil real (I4), y es también el único punto de re-evaluación del
        filtro de libro fino: expulsa del universo activo a un símbolo cuyo
        perfil recalculado ha caído por debajo de
        `cfg.universe.min_profile_median_volume`, y le da a un símbolo ya
        expulsado la oportunidad de volver (`_reevaluar_rechazados`, ver su
        docstring sobre por qué la cadencia diaria y no la de 15 min del
        refresco de universo).

        Sin poda, `candles_1m` crece sin límite (~216k filas/día a 150
        símbolos, spec §9) y `latest_ts`/`load` se degradan con la tabla.
        Sin recálculo diario, el perfil de un proceso de larga vida queda
        anclado para siempre al que se descargó en su primer arranque
        (spec §4.3/§6.1: `profile_builder | arranque + diario`), comparando
        el volumen de hoy contra un baseline cada vez más viejo.

        La ventana de poda y la de recálculo comparten el mismo límite
        (`now_ms - history_days` días): podar primero y recalcular después
        con esa misma frontera es consistente, no hace falta ningún ajuste
        adicional entre ambos pasos.

        Los símbolos aún en `_placeholder_symbols` (bootstrap real en
        vuelo, ver `apply_universe`) se saltan: recalcular aquí con lo poco
        que hubiera en SQLite pisaría el resultado del bootstrap de fondo
        si terminara justo después, y ese símbolo ya se recalculará solo en
        el próximo ciclo de mantenimiento una vez tenga perfil real.

        I-3: nada de esto tenía ningún `await` -medido: `candle_repo.load`
        de 14 días x 150 símbolos ≈ 9.1s más `build_profile` x150 ≈ 2.0s-,
        así que corría entero de un tirón y bloqueaba el hilo del event
        loop durante ~12s seguidos: ni el WS, ni `poll_tickers`, ni el
        dashboard, ni el propio latido de `BitgetWebsocket` podían avanzar
        mientras tanto. `sqlite3.Connection` se abre con
        `check_same_thread=False` justo para permitir esto: el trabajo
        síncrono por símbolo (I/O de disco + CPU de `build_profile`) se
        delega a `asyncio.to_thread`, uno por símbolo -no todos a la vez-,
        para que el event loop recupere el control entre cada uno.
        """
        desde = now_ms - self.cfg.profile.history_days * DIA_MS
        await asyncio.to_thread(self.candle_repo.prune, desde)

        for simbolo in list(self.profiles):
            if simbolo in self._placeholder_symbols:
                continue
            perfil = await asyncio.to_thread(
                self._recalcular_perfil_de_mantenimiento, simbolo, desde, now_ms
            )
            if perfil is None:
                continue
            # el filtro de libro fino es una propiedad del libro, no un
            # evento de una sola vez en el bootstrap: un símbolo activo
            # cuyo perfil se ha ido secando debe salir aquí igual que uno
            # nuevo lo hace en apply_universe (ver `_admite_libro`).
            if not await self._admite_libro(simbolo, perfil):
                continue
            self.profiles[simbolo] = perfil
            self.dirty.add(simbolo)

        await self._reevaluar_rechazados(now_ms)

    def _recalcular_perfil_de_mantenimiento(
        self, symbol: str, desde: int, now_ms: int
    ) -> VolumeProfile | None:
        """Cuerpo síncrono de un símbolo de `run_maintenance` (I-3).

        Aislado en su propio método para que `asyncio.to_thread` pueda
        ejecutarlo en un hilo aparte; no toca ningún estado del orquestador
        directamente (`self.profiles`/`self.dirty`) -eso lo hace el
        llamador, de vuelta en el hilo del event loop, para no mutar
        estructuras compartidas desde un hilo de fondo-. Devuelve `None` si
        no hay velas que recalcular, igual que hacía el bucle original.
        """
        velas = self.candle_repo.load(symbol, desde)
        if not velas:
            return None
        perfil = build_profile(symbol, velas, self.cfg.profile)
        self.profile_repo.save(perfil, now_ms)
        return perfil
