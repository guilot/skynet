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
from scanner_volumen.config import load_config
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import (
    CandleRepo, ProfileRepo, SignalRepo, SupplyRepo,
)
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


async def paso_evaluador(orq: Orchestrator, bootstrapper: Bootstrapper, ahora: int) -> None:
    """Un paso de `bucle_evaluador`: drena reconexiones, evalúa y refresca
    el progreso del bootstrap. Ver `paso_tickers` sobre por qué está
    extraído como función con nombre (I-5)."""
    orq.state.now_ms = ahora  # I-2(a): "ahora" del exchange, para el dashboard
    for simbolo in list(orq.reconnected):
        orq.reconnected.discard(simbolo)
        await orq.refill_gap(simbolo, ahora)
    for t in orq.evaluate(ahora):
        if t.should_alert:
            log.info("ALERTA %s %s score=%.1f", t.symbol, t.current.value, t.score)
    hecho, total = bootstrapper.progress()
    orq.state.bootstrap_done, orq.state.bootstrap_total = hecho, total


async def paso_outcomes(tracker: OutcomeTracker, ahora: int) -> None:
    """Un paso de `bucle_outcomes`. Ver `paso_tickers` (I-5)."""
    try:
        tracker.run_once(ahora)
    except Exception as exc:  # noqa: BLE001
        log.warning("seguimiento de resultados fallido: %s", exc)


async def paso_mantenimiento(orq: Orchestrator, ahora: int) -> None:
    """Un paso de `bucle_mantenimiento` (I2 poda + I4 recálculo). Ver
    `paso_tickers` (I-5)."""
    try:
        await orq.run_maintenance(ahora)
    except Exception as exc:  # noqa: BLE001
        log.warning("mantenimiento diario fallido: %s", exc)


def marcar_ws_conectado(state, conectado: bool) -> None:
    """Callback de `BitgetWebsocket.run` (I1): mueve `state.ws_connected`.

    Función independiente (en vez de una closure anidada en `main()`, como
    antes) para poder probarla directamente con un `ScannerState` de
    prueba, sin construir un `Orchestrator` completo."""
    state.ws_connected = conectado


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    cfg = load_config(Path("config.toml"))
    conn = open_db(Path(cfg.server.db_path))

    candle_repo = CandleRepo(conn)
    profile_repo = ProfileRepo(conn)
    signal_repo = SignalRepo(conn)
    supply_repo = SupplyRepo(conn)

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=20.0) as http:
        rest = BitgetRest(cfg.market.venue, cfg.rest.rate_limit_per_second, http)
        ws = BitgetWebsocket(cfg.market.venue)
        supply = SupplyCache(supply_repo, http, refresh_hours=cfg.supply.refresh_hours)
        bootstrapper = Bootstrapper(rest, candle_repo, profile_repo, cfg.profile)
        selector = UniverseSelector(cfg.universe)
        orq = Orchestrator(cfg, rest, ws, candle_repo, profile_repo,
                            signal_repo, supply, bootstrapper)
        orq.state.stale_after_ms = int(cfg.dashboard.stale_after_seconds * 1000)
        tracker = OutcomeTracker(
            signal_repo, candle_repo, horizons=cfg.outcomes.horizons_minutes
        )

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
                await paso_evaluador(orq, bootstrapper, ahora)
                await asyncio.sleep(cfg.engine.tick_seconds)

        async def bucle_outcomes() -> None:
            while True:
                await asyncio.sleep(cfg.outcomes.poll_seconds)
                await paso_outcomes(tracker, orq.now_ms(ahora_ms()))

        async def bucle_mantenimiento() -> None:
            """Poda diaria (I2) y recálculo diario del perfil de volumen (I4).

            `interval_hours` viene de `config.toml` (`[maintenance]`): todo
            umbral/cadencia de negocio vive ahí, no hardcodeado aquí.
            """
            while True:
                await asyncio.sleep(cfg.maintenance.interval_hours * 3600)
                await paso_mantenimiento(orq, orq.now_ms(ahora_ms()))

        log.info("dashboard en http://%s:%d", cfg.server.host, cfg.server.port)
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
