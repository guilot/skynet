# scanner_volumen/__main__.py
"""Punto de entrada: arranca todas las tareas del scanner.

Es el único sitio donde `time.time()` es legítimo (ver CLAUDE.md/constraints
del proyecto): el resto del sistema recibe siempre `now_ms` como parámetro,
nunca lee el reloj por su cuenta, para que el comportamiento sea
reproducible en tests.

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
    conn = open_db(Path("data/scanner.db"))

    candle_repo = CandleRepo(conn)
    profile_repo = ProfileRepo(conn)
    signal_repo = SignalRepo(conn)
    supply_repo = SupplyRepo(conn)

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=20.0) as http:
        rest = BitgetRest(cfg.market.venue, cfg.rest.rate_limit_per_second, http)
        ws = BitgetWebsocket(cfg.market.venue)
        supply = SupplyCache(supply_repo, http)
        bootstrapper = Bootstrapper(rest, candle_repo, profile_repo, cfg.profile)
        selector = UniverseSelector(cfg.universe)
        orq = Orchestrator(cfg, rest, ws, candle_repo, profile_repo,
                            signal_repo, supply, bootstrapper)
        tracker = OutcomeTracker(signal_repo, candle_repo)

        app = create_app(orq.state, signal_repo)
        servidor = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="warning")
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
                ahora = ahora_ms()
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
                ahora = ahora_ms()
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
                await asyncio.sleep(60)
                try:
                    tracker.run_once(ahora_ms())
                except Exception as exc:  # noqa: BLE001
                    log.warning("seguimiento de resultados fallido: %s", exc)

        log.info("dashboard en http://127.0.0.1:8000")
        await asyncio.gather(
            ws.run(orq.handle_ws_event),
            bucle_tickers(),
            bucle_evaluador(),
            bucle_outcomes(),
            servidor.serve(),
        )


if __name__ == "__main__":
    asyncio.run(main())
