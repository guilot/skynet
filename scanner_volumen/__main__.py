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

            El refresco de ticker en sí se delega en `orq.poll_tickers`
            (probado en Task 16): marca sucios los símbolos que ya tienen
            buffer y nunca propaga un fallo de REST. La selección de
            universo no puede delegarse igual porque necesita la lista
            completa de tickers -incluidos símbolos que el orquestador
            todavía no conoce- que `poll_tickers` no expone.
            """
            ultimo_universo = 0
            while True:
                ahora = orq.now_ms(ahora_ms())
                try:
                    await orq.poll_tickers(ahora)
                    if ahora - ultimo_universo >= cfg.universe.refresh_minutes * 60_000:
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
                await asyncio.sleep(cfg.engine.ticker_poll_seconds)

        async def bucle_evaluador() -> None:
            while True:
                ahora = orq.now_ms(ahora_ms())
                for simbolo in list(orq.reconnected):
                    orq.reconnected.discard(simbolo)
                    await orq.refill_gap(simbolo, ahora)
                for t in orq.evaluate(ahora):
                    if t.should_alert:
                        log.info("ALERTA %s %s score=%.1f", t.symbol,
                                  t.current.value, t.score)
                hecho, total = bootstrapper.progress()
                orq.state.bootstrap_done, orq.state.bootstrap_total = hecho, total
                await asyncio.sleep(cfg.engine.tick_seconds)

        async def bucle_outcomes() -> None:
            while True:
                await asyncio.sleep(cfg.outcomes.poll_seconds)
                try:
                    tracker.run_once(orq.now_ms(ahora_ms()))
                except Exception as exc:  # noqa: BLE001
                    log.warning("seguimiento de resultados fallido: %s", exc)

        async def bucle_mantenimiento() -> None:
            """Poda diaria (I2) y recálculo diario del perfil de volumen (I4).

            `interval_hours` viene de `config.toml` (`[maintenance]`): todo
            umbral/cadencia de negocio vive ahí, no hardcodeado aquí.
            """
            while True:
                await asyncio.sleep(cfg.maintenance.interval_hours * 3600)
                try:
                    await orq.run_maintenance(orq.now_ms(ahora_ms()))
                except Exception as exc:  # noqa: BLE001
                    log.warning("mantenimiento diario fallido: %s", exc)

        def _marcar_ws_conectado(conectado: bool) -> None:
            orq.state.ws_connected = conectado

        log.info("dashboard en http://%s:%d", cfg.server.host, cfg.server.port)
        await asyncio.gather(
            ws.run(orq.handle_ws_event, on_connection_change=_marcar_ws_conectado),
            bucle_tickers(),
            bucle_evaluador(),
            bucle_outcomes(),
            bucle_mantenimiento(),
            servidor.serve(),
        )


if __name__ == "__main__":
    asyncio.run(main())
