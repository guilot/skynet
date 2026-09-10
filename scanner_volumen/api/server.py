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
        cerradas = []
        for f in bot_repo.cerradas(modo, limite=20):
            fills = bot_repo.fills_de(f["id"])
            # El precio de salida de una posicion con salidas escalonadas no
            # es un numero: son dos o tres. Se expone la MEDIA PONDERADA por
            # la fraccion cobrada en cada una -el precio unico al que habria
            # dado lo mismo cerrar todo de golpe-, y el desglose completo
            # vive en /api/bot/trade/{id}. Poner "el ultimo" seria mas
            # simple y mentiria: en un trade que escalo a EXTREME, el ultimo
            # tramo suele ser el mas pequeno.
            cobrado = sum(x["fraction"] for x in fills)
            salida = (sum(x["precio"] * x["fraction"] for x in fills) / cobrado
                      if cobrado > 0 else None)
            cerradas.append({
                "id": f["id"], "symbol": f["symbol"], "direction": f["direction"],
                "entry_ts": f["entry_ts"], "entry_price": f["entry_price"],
                "close_ts": f["close_ts"], "exit_price": salida,
                # `fees` YA es el coste total del trade: `fees_acumuladas` se
                # inicializa con la comision de ENTRADA (`runner.py`, `_abrir`)
                # y `_acumular_pnl` le va sumando las de cada salida. Sumarle
                # `fee_entrada` aqui la contaba dos veces -era un defecto de
                # este panel, no del bot, y lo detecto el usuario al ver que
                # el total no cuadraba con las fases.
                "pnl": f["pnl"], "fees": f["fees"],
                "margin": f["margin"], "max_rank": f["max_rank"],
                "fases": len(fills), "degradada": bool(f["degradada"]),
            })
        return {
            "activo": True, "modo": modo, "equity": bot_repo.equity(modo),
            "saldo_real": bot_repo.saldo_real(modo),
            "abiertas": abiertas, "cerradas": cerradas,
        }

    @app.get("/api/bot/trade/{trade_id}")
    def get_trade(trade_id: int) -> dict:
        """Desglose de UN trade por fases: cuanto se cobro en cada una, a que
        precio y con cuanto PnL.

        Va en su propia ruta y no dentro de `/api/bot` a proposito: ese lo
        sondea el panel cada pocos segundos, y meterle los fills de veinte
        posiciones lo engordaria en cada vuelta para un dato que solo se
        mira al pinchar.

        El PnL por fase se RECONSTRUYE aqui (no esta guardado) a partir del
        precio de entrada, el tamano y la fraccion cobrada. Se comprobo que
        la suma reproduce exactamente el `pnl` que el bot guardo en la
        posicion, asi que no es una estimacion: es la misma cuenta,
        desglosada."""
        if bot_repo is None:
            raise HTTPException(status_code=404, detail="bot desactivado")
        fila = bot_repo.posicion(trade_id)
        if fila is None or fila["modo"] != modo:
            raise HTTPException(status_code=404, detail=f"trade {trade_id} desconocido")

        signo = 1.0 if fila["direction"] == "LONG" else -1.0
        fases = []
        for f in bot_repo.fills_de(trade_id):
            bruto = ((f["precio"] - fila["entry_price"]) * fila["size"]
                     * f["fraction"] * signo)
            # `precio_regla` es el nivel que la REGLA pedia; `precio` es el
            # que dio el mercado. La diferencia es el deslizamiento, y es lo
            # que toda esta fase existe para medir. Puede no haberlo (una
            # salida a mercado por temporizador no promete ningun nivel).
            regla = f["precio_regla"]
            desvio = (10_000 * (f["precio"] - regla) / regla * signo
                      if regla else None)
            fases.append({
                "reason": f["reason"], "ts": f["ts"], "fraction": f["fraction"],
                "precio": f["precio"], "precio_regla": regla,
                "desvio_bps": desvio, "pnl_bruto": bruto,
                "comision": f["comision"], "pnl_neto": bruto - f["comision"],
                "cierre_exchange": bool(f["cierre_exchange"]),
                "tardio": bool(f["tardio"]),
            })
        return {
            "id": trade_id, "symbol": fila["symbol"],
            "direction": fila["direction"], "entry_price": fila["entry_price"],
            "entry_ts": fila["entry_ts"], "close_ts": fila["close_ts"],
            "size": fila["size"], "margin": fila["margin"],
            "fee_entrada": fila["fee_entrada"],
            # Ver la nota de `fees` mas arriba: la columna ya incluye la
            # entrada, asi que el total es esa columna tal cual.
            "fees_total": fila["fees"],
            "pnl": fila["pnl"], "max_rank": fila["max_rank"],
            "degradada": bool(fila["degradada"]),
            "fases": fases,
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
