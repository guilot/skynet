# scanner_volumen/__main__.py
"""Punto de entrada: monta las piezas y arranca el escáner en vivo.

Sin tests propios (todo el comportamiento que importa ya está cubierto por
`Orchestrator`, probado con dobles de red); esto solo hace la instalación con
dependencias reales y delega el bucle a `Orchestrator.run()`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx

from scanner_volumen.app.bootstrap import Bootstrapper
from scanner_volumen.app.orchestrator import Orchestrator
from scanner_volumen.bitget.rest import BitgetRest
from scanner_volumen.bitget.ws import BitgetWebsocket
from scanner_volumen.config import load_config
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import (
    CandleRepo, ProfileRepo, SignalRepo, SupplyRepo,
)
from scanner_volumen.universe.selector import UniverseSelector
from scanner_volumen.universe.supply import SupplyCache

CONFIG_PATH = Path("config.toml")
DB_PATH = Path("scanner.db")

log = logging.getLogger(__name__)


async def _seed_universe(
    orq: Orchestrator, selector: UniverseSelector, rest: BitgetRest, now_ms: int
) -> None:
    """Selecciona el universo inicial y dispara el bootstrap de histórico de
    cada símbolo (velas + perfil de volumen) antes de suscribir el WebSocket."""
    contratos = await rest.get_contracts()
    tickers = await rest.get_tickers()
    update = selector.select(contratos, tickers, now_ms)
    orq.bootstrapper.expect(update.ordered)
    await orq.apply_universe(update, now_ms)
    for ticker in tickers:
        if ticker.symbol in orq.buffers or ticker.symbol in update.symbols:
            orq.set_ticker(ticker)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = load_config(CONFIG_PATH)
    conn = open_db(DB_PATH)

    async with httpx.AsyncClient(timeout=10.0) as client:
        rest = BitgetRest(cfg.market.venue, cfg.rest.rate_limit_per_second, client)
        ws = BitgetWebsocket(cfg.market.venue)

        candle_repo = CandleRepo(conn)
        profile_repo = ProfileRepo(conn)
        signal_repo = SignalRepo(conn)
        supply_repo = SupplyRepo(conn)

        supply = SupplyCache(supply_repo, client)
        bootstrapper = Bootstrapper(rest, candle_repo, profile_repo, cfg.profile)
        selector = UniverseSelector(cfg.universe)

        orq = Orchestrator(
            cfg=cfg, rest=rest, ws=ws,
            candle_repo=candle_repo, profile_repo=profile_repo,
            signal_repo=signal_repo, supply=supply, bootstrapper=bootstrapper,
        )

        ahora = int(time.time() * 1000)
        log.info("seleccionando universo inicial y descargando histórico...")
        await _seed_universe(orq, selector, rest, ahora)
        await supply.refresh(list(orq.buffers), ahora)
        log.info("bootstrap completo: %d/%d símbolos", *orq.bootstrapper.progress())

        await ws.subscribe(sorted(orq.buffers))
        await orq.run()


if __name__ == "__main__":
    asyncio.run(main())
