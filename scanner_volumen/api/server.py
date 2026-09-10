# scanner_volumen/api/server.py
"""API HTTP y WebSocket del dashboard.

El estado ya está calculado por el orquestador: aquí solo se serializa. El
dashboard no dispara ningún cálculo.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from scanner_volumen.app.state import ScannerState
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.storage.repos import SignalRepo

ESTATICOS = Path(__file__).parent / "static"
INTERVALO_PUSH = 1.0

log = logging.getLogger(__name__)


def create_app(
    state: ScannerState, signal_repo: SignalRepo,
    bot_repo: BotRepo | None = None, modo: str = "paper",
) -> FastAPI:
    """`bot_repo` y `modo` tienen valor por defecto para que ningún llamador
    existente (ni los tests que ya construían `create_app` con dos
    argumentos) se rompa. Con el bot desactivado (`bot_repo=None`, el caso
    de siempre hasta ahora) `/api/bot` responde `{"activo": False, ...}`
    sin reventar."""
    app = FastAPI(title="Bitget Momentum Scanner")

    @app.get("/api/state")
    def get_state() -> dict:
        return state.to_dict()

    @app.get("/api/symbol/{symbol}")
    def get_symbol(symbol: str) -> dict:
        snap = state.snapshot(symbol.upper())
        if snap is None:
            raise HTTPException(status_code=404, detail=f"símbolo desconocido: {symbol}")
        return snap.to_dict()

    @app.get("/api/signals")
    def get_signals(since: int = 0) -> list[dict]:
        return signal_repo.recent(since_ms=since)

    @app.get("/api/bot")
    def get_bot() -> dict:
        """Estado del bot para el panel del dashboard. Solo serializa: como
        el resto de la API, no dispara ningún cálculo.

        `precio` va siempre a `None`: `ScannerState` no expone un precio en
        vivo por símbolo (solo lo tiene el `Orchestrator`, vía `_precio_de`
        en `__main__`), y esta tarea no le añade ese método.

        `saldo_real` (Task 11, ronda de arreglo): el último saldo real
        persistido por `BotRepo.set_saldo_real` -`None` en `paper` (nunca se
        persiste) y en `real` hasta el primer tick del proceso en vivo. Sin
        esto, el panel avisaba de "DINERO REAL" junto a un número que en
        realidad era el equity CONTABLE, no el saldo real -exactamente la
        confusión que el aviso visual existe para evitar."""
        if bot_repo is None:
            return {
                "activo": False, "equity": None, "saldo_real": None,
                "abiertas": [], "cerradas": [],
            }
        abiertas = [
            {
                "symbol": fila["symbol"], "direction": fila["direction"],
                "entry_ts": fila["entry_ts"], "entry_price": fila["entry_price"],
                "precio": None, "margin": fila["margin"],
            }
            for fila in bot_repo.abiertas(modo)
        ]
        # `limite=20` empuja el recorte a la consulta SQL (ORDER BY ...
        # DESC LIMIT ?, ver BotRepo.cerradas): sin él, cada sondeo del
        # panel (cada 5s) traería y reordenaría la tabla `bot_posiciones`
        # entera solo para descartar casi todo en Python. La consulta ya
        # devuelve las más recientes primero, así que se expone tal cual
        # -sin `reversed`- para que el panel muestre el último cierre
        # arriba.
        # Historico de trades para el panel. Se enriquece respecto a lo que
        # habia (symbol/close_ts/pnl/max_rank) con lo que hace falta para
        # LEERLO como un historial y no como una lista de numeros: la
        # direccion, el precio de entrada y el instante de entrada -del que
        # sale la duracion-. `degradada` viaja porque una posicion cerrada
        # tras un fallo no es un trade normal y el panel no debe presentarla
        # como tal.
        cerradas = [
            {"symbol": f["symbol"], "direction": f["direction"],
             "entry_ts": f["entry_ts"], "entry_price": f["entry_price"],
             "close_ts": f["close_ts"], "pnl": f["pnl"], "fees": f["fees"],
             "margin": f["margin"], "max_rank": f["max_rank"],
             "degradada": bool(f["degradada"])}
            for f in bot_repo.cerradas(modo, limite=20)
        ]
        return {
            "activo": True, "modo": modo, "equity": bot_repo.equity(modo),
            "saldo_real": bot_repo.saldo_real(modo),
            "abiertas": abiertas, "cerradas": cerradas,
        }

    @app.websocket("/ws")
    async def ws_estado(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(state.to_dict())
                await asyncio.sleep(INTERVALO_PUSH)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001 - una pestaña caída no tumba el servidor
            log.debug("WebSocket del dashboard cerrado: %s", exc)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(ESTATICOS / "index.html")

    with contextlib.suppress(RuntimeError):
        app.mount("/static", StaticFiles(directory=ESTATICOS), name="static")

    return app
