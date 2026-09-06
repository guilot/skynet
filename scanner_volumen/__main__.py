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
import logging
import time
from pathlib import Path

import httpx
import uvicorn

from scanner_volumen.api.server import create_app
from scanner_volumen.app.bootstrap import Bootstrapper
from scanner_volumen.app.orchestrator import Orchestrator
from scanner_volumen.app.outcomes import OutcomeTracker
from scanner_volumen.bitget.rest import BASE_URL, BitgetRest
from scanner_volumen.bitget.ws import BitgetWebsocket
from scanner_volumen.bot.broker import PaperBroker
from scanner_volumen.bot.portfolio import LivePortfolio
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.bot.runner import BotRunner
from scanner_volumen.config import load_config
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
) -> None:
    """Un paso de `bucle_evaluador`: drena reconexiones, evalúa y refresca
    el progreso del bootstrap. Ver `paso_tickers` sobre por qué está
    extraído como función con nombre (I-5).

    `bot` es opcional (`None` por defecto) para que con `bot.enabled = false`
    -el valor por defecto de `config.toml`- el escáner se comporte
    exactamente como antes de que existiera el bot: todas las llamadas
    existentes a `paso_evaluador` siguen funcionando sin cambios."""
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
            await bot.on_tick(orq.transiciones_evaluadas, _precio_de(orq), ahora)
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
        if cfg.bot.enabled:
            bot_repo = BotRepo(conn)
            # fija el capital la primera vez y respeta el ya guardado en
            # arranques posteriores: el saldo es un valor vivo, no se
            # reinicia en cada despliegue.
            bot_repo.set_equity_inicial(
                bot_repo.equity_inicial(defecto=cfg.bot.equity_inicial)
            )
            params = StrategyParams()
            bot = BotRunner(params, cfg.bot, bot_repo, PaperBroker(params),
                            LivePortfolio(params, cfg.bot, bot_repo))
            log.info("bot ACTIVO en modo %s, equity %.2f",
                     cfg.bot.modo, bot_repo.equity(cfg.bot.modo))
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

            await bot.reconstruir(_transiciones_de, _velas_de, _precio_de(orq),
                                  orq.now_ms(ahora_ms()))

        app = create_app(orq.state, signal_repo)
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
                await paso_evaluador(orq, bootstrapper, ahora, bot)
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

        log.info(
            "config=%s dashboard en http://%s:%d",
            args.config, cfg.server.host, cfg.server.port,
        )
        await asyncio.gather(
            ws.run(
                orq.handle_ws_event,
                on_connection_change=lambda c: marcar_ws_conectado(orq.state, c),
            ),
            bucle_tickers(),
            bucle_evaluador(),
            bucle_outcomes(),
            bucle_mantenimiento(),
            servidor.serve(),
        )


if __name__ == "__main__":
    asyncio.run(main())
