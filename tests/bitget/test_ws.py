import asyncio
import json
from pathlib import Path

import pytest

from scanner_volumen.bitget import ws as ws_mod
from scanner_volumen.bitget.ws import (
    BitgetWebsocket,
    build_subscribe,
    build_unsubscribe,
    decode_message,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


def cargar_sesion() -> list[dict]:
    return json.loads((FIXTURES / "ws_candle1m_session.json").read_text())


def test_build_subscribe_genera_un_arg_por_simbolo():
    msg = build_subscribe(["BTCUSDT", "ETHUSDT"], venue="USDT-FUTURES")
    assert msg["op"] == "subscribe"
    assert len(msg["args"]) == 2
    assert msg["args"][0] == {
        "instType": "USDT-FUTURES", "channel": "candle1m", "instId": "BTCUSDT",
    }
    assert msg["args"][1] == {
        "instType": "USDT-FUTURES", "channel": "candle1m", "instId": "ETHUSDT",
    }


def test_build_unsubscribe_usa_la_operacion_correcta():
    msg = build_unsubscribe(["BTCUSDT"], venue="USDT-FUTURES")
    assert msg["op"] == "unsubscribe"
    assert msg["args"][0] == {
        "instType": "USDT-FUTURES", "channel": "candle1m", "instId": "BTCUSDT",
    }


def test_decode_de_pong():
    ev = decode_message("pong")
    assert ev.kind == "pong"
    assert ev.symbol is None
    assert ev.candles == []


def test_decode_de_confirmacion_de_suscripcion():
    raw = json.dumps({"event": "subscribe",
                      "arg": {"instType": "USDT-FUTURES", "channel": "candle1m",
                              "instId": "BTCUSDT"}})
    ev = decode_message(raw)
    assert ev.kind == "subscribed"
    assert ev.symbol == "BTCUSDT"
    assert ev.candles == []


def test_decode_de_error():
    raw = json.dumps({"event": "error", "code": "30001", "msg": "channel not exist"})
    ev = decode_message(raw)
    assert ev.kind == "error"
    assert ev.symbol is None
    # el detalle del error debe conservarse para poder registrarlo/depurarlo
    assert ev.raw["code"] == "30001"
    assert ev.raw["msg"] == "channel not exist"


def test_decode_de_snapshot_devuelve_todas_las_velas():
    mensajes = cargar_sesion()
    snapshot = next(m for m in mensajes if m.get("action") == "snapshot")
    ev = decode_message(json.dumps(snapshot))
    assert ev.kind == "snapshot"
    assert ev.symbol == snapshot["arg"]["instId"]
    assert len(ev.candles) == len(snapshot["data"])
    # primera y última vela deben coincidir exactamente con la fila cruda,
    # no solo tener un timestamp positivo (eso lo pasaría cualquier parseo roto)
    primera, ultima = ev.candles[0], ev.candles[-1]
    assert primera.ts == int(snapshot["data"][0][0])
    assert primera.open == float(snapshot["data"][0][1])
    assert primera.close == float(snapshot["data"][0][4])
    assert primera.quote_vol == float(snapshot["data"][0][6])
    assert ultima.ts == int(snapshot["data"][-1][0])


def test_decode_de_update_parsea_velas_de_ocho_campos():
    mensajes = cargar_sesion()
    update = next(m for m in mensajes if m.get("action") == "update")
    ev = decode_message(json.dumps(update))
    assert ev.kind == "update"
    assert ev.symbol == update["arg"]["instId"]
    assert len(ev.candles) == len(update["data"])
    fila = update["data"][0]
    vela = ev.candles[0]
    assert vela.ts == int(fila[0])
    assert vela.open == float(fila[1])
    assert vela.high == float(fila[2])
    assert vela.low == float(fila[3])
    assert vela.close == float(fila[4])
    assert vela.base_vol == float(fila[5])
    assert vela.quote_vol == float(fila[6])


def test_toda_la_sesion_grabada_se_decodifica_sin_errores():
    mensajes = cargar_sesion()
    tipos = {}
    for m in mensajes:
        ev = decode_message(json.dumps(m))
        assert ev is not None
        tipos[ev.kind] = tipos.get(ev.kind, 0) + 1
    assert tipos["subscribed"] == 3
    assert tipos["snapshot"] == 3
    assert tipos["update"] == 24
    assert sum(tipos.values()) == len(mensajes)


def test_decode_de_json_invalido_devuelve_none():
    assert decode_message("{esto no es json}") is None


def test_decode_de_json_valido_pero_no_objeto_devuelve_none():
    """Un array o número JSON válido no es un mensaje de Bitget reconocible."""
    assert decode_message("[1, 2, 3]") is None
    assert decode_message("42") is None


def test_decode_de_mensaje_sin_event_ni_action_devuelve_none():
    """Un objeto JSON que no encaja en ningún tipo conocido no debe
    camuflarse como 'pong': se descarta explícitamente."""
    raw = json.dumps({"algo": "inesperado"})
    assert decode_message(raw) is None


def test_decode_de_vela_corrupta_en_snapshot_se_descarta_sin_romper_las_demas():
    """Una fila de vela inválida dentro del batch se ignora; el resto se
    decodifica igual (parse_candle exige 7 u 8 campos)."""
    mensajes = cargar_sesion()
    snapshot = next(m for m in mensajes if m.get("action") == "snapshot")
    corrupto = json.loads(json.dumps(snapshot))
    corrupto["data"] = [["solo", "cuatro", "campos", "aqui"], *corrupto["data"]]
    ev = decode_message(json.dumps(corrupto))
    assert ev.kind == "snapshot"
    assert len(ev.candles) == len(snapshot["data"])


class _ConexionFalsa:
    """Simula el objeto que devuelve `websockets.connect(...)`: un context
    manager async que además es iterable async sobre mensajes ya decididos."""

    def __init__(self, mensajes: list[str]) -> None:
        self._mensajes = mensajes
        self.enviados: list[str] = []

    async def __aenter__(self) -> "_ConexionFalsa":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def __aiter__(self):
        return self._generador()

    async def _generador(self):
        for m in self._mensajes:
            yield m

    async def send(self, data: str) -> None:
        self.enviados.append(data)


async def test_run_vuelve_a_suscribir_todo_tras_reconectar(monkeypatch):
    """Sin abrir ningún socket real: inyectamos un connect_factory que agota
    su lista de mensajes (simulando una caída) y comprobamos que la segunda
    conexión recibe de nuevo la suscripción completa."""
    monkeypatch.setattr(ws_mod, "BACKOFF_INICIAL", 0.01)
    monkeypatch.setattr(ws_mod, "BACKOFF_MAXIMO", 0.01)

    conexiones = [_ConexionFalsa([]), _ConexionFalsa([])]
    creadas: list[_ConexionFalsa] = []

    def fabrica(url: str) -> _ConexionFalsa:
        creadas.append(conexiones.pop(0))
        return creadas[-1]

    cliente = BitgetWebsocket(venue="USDT-FUTURES", url="wss://fake", connect_factory=fabrica)
    await cliente.subscribe(["BTCUSDT"])

    async def on_event(_ev):
        pass

    tarea = asyncio.create_task(cliente.run(on_event))
    try:
        for _ in range(300):
            if len(creadas) >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea

    assert len(creadas) == 2
    for conexion in creadas:
        assert len(conexion.enviados) == 1
        enviado = json.loads(conexion.enviados[0])
        assert enviado["op"] == "subscribe"
        assert enviado["args"][0]["instId"] == "BTCUSDT"
