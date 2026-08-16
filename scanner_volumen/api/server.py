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
from scanner_volumen.storage.repos import SignalRepo

ESTATICOS = Path(__file__).parent / "static"
INTERVALO_PUSH = 1.0

log = logging.getLogger(__name__)


def create_app(state: ScannerState, signal_repo: SignalRepo) -> FastAPI:
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
