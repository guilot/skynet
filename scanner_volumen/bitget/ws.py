"""Cliente WebSocket público de Bitget.

El protocolo y el decodificado se separan de la conexión: `decode_message` y
`build_subscribe` son funciones puras que se prueban con la sesión grabada en
los fixtures, sin abrir un socket.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import websockets

from scanner_volumen.bitget.parsing import parse_candle
from scanner_volumen.models import Candle

URL_PUBLICA = "wss://ws.bitget.com/v2/ws/public"
INTERVALO_PING = 25.0
BACKOFF_INICIAL = 1.0
BACKOFF_MAXIMO = 60.0

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WsEvent:
    kind: str  # "subscribed" | "snapshot" | "update" | "error" | "pong"
    symbol: str | None = None
    candles: list[Candle] = field(default_factory=list)
    raw: dict | None = None


def _args(symbols: list[str], venue: str, channel: str) -> list[dict]:
    return [
        {"instType": venue, "channel": channel, "instId": s} for s in symbols
    ]


def build_subscribe(
    symbols: list[str], venue: str, channel: str = "candle1m"
) -> dict:
    return {"op": "subscribe", "args": _args(symbols, venue, channel)}


def build_unsubscribe(
    symbols: list[str], venue: str, channel: str = "candle1m"
) -> dict:
    return {"op": "unsubscribe", "args": _args(symbols, venue, channel)}


def _decodificar_velas(filas: list[list[str]]) -> list[Candle]:
    velas = []
    for fila in filas:
        try:
            velas.append(parse_candle(fila))
        except (ValueError, TypeError):
            log.warning("vela WS descartada: %r", fila)
    return velas


def decode_message(raw: str) -> WsEvent | None:
    """Decodifica un mensaje crudo del WebSocket público de Bitget.

    Devuelve `None` (y registra un warning) ante cualquier mensaje que no se
    pueda interpretar: JSON inválido, JSON válido pero no-objeto, o un objeto
    sin `event` ni `action` reconocidos. Nunca lanza: un mensaje corrupto no
    debe tumbar el bucle de lectura.
    """
    if raw == "pong":
        return WsEvent(kind="pong")

    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("mensaje WS no parseable como JSON: %r", raw[:200])
        return None

    if not isinstance(msg, dict):
        log.warning("mensaje WS no es un objeto JSON: %r", raw[:200])
        return None

    evento = msg.get("event")
    if evento == "subscribe":
        arg = msg.get("arg") or {}
        return WsEvent(kind="subscribed", symbol=arg.get("instId"), raw=msg)
    if evento == "error":
        return WsEvent(kind="error", raw=msg)

    accion = msg.get("action")
    if accion in ("snapshot", "update"):
        arg = msg.get("arg") or {}
        velas = _decodificar_velas(msg.get("data") or [])
        return WsEvent(kind=accion, symbol=arg.get("instId"), candles=velas, raw=msg)

    log.warning("mensaje WS de tipo desconocido, se descarta: %r", msg)
    return None


class BitgetWebsocket:
    """Mantiene la conexión viva y vuelve a suscribir todo tras reconectar.

    El cliente es dueño del conjunto de símbolos suscritos (`_symbols`): cada
    vez que se abre una conexión nueva (la primera o tras una caída) se
    reenvía la suscripción completa, porque el servidor no recuerda nada de
    la conexión anterior.
    """

    def __init__(
        self,
        venue: str,
        url: str = URL_PUBLICA,
        connect_factory: Callable[[str], object] | None = None,
    ) -> None:
        self._venue = venue
        self._url = url
        self._connect = connect_factory or (
            lambda u: websockets.connect(u, ping_interval=None)
        )
        self._symbols: set[str] = set()
        self._ws = None
        self._lock = asyncio.Lock()

    async def subscribe(self, symbols: list[str]) -> None:
        nuevos = [s for s in symbols if s not in self._symbols]
        self._symbols.update(nuevos)
        if nuevos and self._ws is not None:
            await self._enviar(build_subscribe(nuevos, self._venue))

    async def unsubscribe(self, symbols: list[str]) -> None:
        quitar = [s for s in symbols if s in self._symbols]
        self._symbols.difference_update(quitar)
        if quitar and self._ws is not None:
            await self._enviar(build_unsubscribe(quitar, self._venue))

    async def _enviar(self, mensaje: dict) -> None:
        async with self._lock:
            if self._ws is not None:
                await self._ws.send(json.dumps(mensaje))

    async def run(
        self,
        on_event: Callable[[WsEvent], Awaitable[None]],
        on_connection_change: Callable[[bool], None] | None = None,
    ) -> None:
        """Mantiene la conexión viva y notifica su salud (I1).

        El WS es la única fuente de velas; sin `on_connection_change` no
        había forma de distinguir "feed muerto" de "mercado tranquilo" desde
        fuera de esta clase. Se llama con `True` justo después de
        (re)suscribir en cada conexión nueva -incluso si `self._symbols`
        está vacío, la conexión en sí ya está viva- y con `False` en el
        `finally` que cubre cualquier salida de la conexión: fallo de red,
        cierre del servidor o cancelación de la tarea. `on_connection_change`
        es síncrono a propósito (solo marca un flag en `ScannerState`, ver
        __main__): no hace falta un callback async para eso.
        """

        def _marcar(conectado: bool) -> None:
            if on_connection_change is not None:
                on_connection_change(conectado)

        backoff = BACKOFF_INICIAL
        while True:
            try:
                async with self._connect(self._url) as ws:
                    self._ws = ws
                    backoff = BACKOFF_INICIAL
                    if self._symbols:
                        await self._enviar(
                            build_subscribe(sorted(self._symbols), self._venue)
                        )
                    _marcar(True)
                    ping = asyncio.create_task(self._latido(ws))
                    try:
                        async for raw in ws:
                            evento = decode_message(raw)
                            if evento is not None and evento.kind != "pong":
                                await on_event(evento)
                    finally:
                        ping.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - se registra y se reintenta
                log.warning("WebSocket caído (%s), reintento en %.0fs", exc, backoff)
            finally:
                self._ws = None
                _marcar(False)
            await asyncio.sleep(backoff)
            backoff = min(BACKOFF_MAXIMO, backoff * 2)

    async def _latido(self, ws) -> None:
        while True:
            await asyncio.sleep(INTERVALO_PING)
            await ws.send("ping")
