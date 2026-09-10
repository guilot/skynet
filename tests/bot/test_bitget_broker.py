"""Tests de `BitgetBroker` contra un doble de `BitgetPrivate`.

Ningún test de este fichero toca la red: `FakeBitgetPrivate` registra las
llamadas que recibe y devuelve datos fabricados, igual que el doble descrito
en el brief de la Task 6.
"""
from __future__ import annotations

import pytest

from scanner_volumen.bitget.private import FillOrden
from scanner_volumen.bot.bitget_broker import BitgetBroker
from scanner_volumen.models import Direction
from scanner_volumen.strategy.model import StrategyParams

MIN = 60_000


class FakeBitgetPrivate:
    """Doble de `BitgetPrivate` que registra cada llamada de escritura.

    `fill` es el fill que `get_fill` devuelve por defecto para cualquier
    orden -deliberadamente distinto del precio/cantidad "solicitados" en
    cada test, para que una implementación que devolviera lo solicitado en
    vez de lo ejecutado se detecte de inmediato.
    """

    def __init__(self, fill: FillOrden | None = None, cancelar_lanza: bool = False):
        self.fill = fill or FillOrden(precio=101.5, cantidad=3.94, comision=0.24)
        self.cancelar_lanza = cancelar_lanza
        self.ordenes: list[dict] = []
        self.stops_colocados: list[dict] = []
        self.stops_movidos: list[dict] = []
        self.stops_cancelados: list[dict] = []
        self._contador_ids = 0

    def _nuevo_id(self) -> str:
        self._contador_ids += 1
        return f"exchange-order-{self._contador_ids}"

    async def colocar_orden(self, symbol, lado, cantidad, reduce_only, client_oid) -> str:
        order_id = self._nuevo_id()
        self.ordenes.append({
            "symbol": symbol, "lado": lado, "cantidad": cantidad,
            "reduce_only": reduce_only, "client_oid": client_oid,
            "order_id": order_id,
        })
        return order_id

    async def colocar_stop(self, symbol, lado, cantidad, precio_disparo, client_oid) -> str:
        stop_id = self._nuevo_id()
        self.stops_colocados.append({
            "symbol": symbol, "lado": lado, "cantidad": cantidad,
            "precio_disparo": precio_disparo, "client_oid": client_oid,
            "stop_id": stop_id,
        })
        return stop_id

    async def mover_stop(self, symbol, stop_id, precio_disparo, cantidad) -> str:
        self.stops_movidos.append({
            "symbol": symbol, "stop_id": stop_id, "precio_disparo": precio_disparo,
            "cantidad": cantidad,
        })
        return stop_id

    async def cancelar_stop(self, symbol, stop_id) -> None:
        self.stops_cancelados.append({"symbol": symbol, "stop_id": stop_id})
        if self.cancelar_lanza:
            raise RuntimeError(
                "Bitget devolvió code=40768 msg=order does not exist "
                "en /api/v2/mix/order/cancel-plan-order"
            )

    async def get_fill(self, symbol, order_id) -> FillOrden:
        return self.fill


def _broker(privado: FakeBitgetPrivate | None = None) -> tuple[BitgetBroker, FakeBitgetPrivate]:
    privado = privado or FakeBitgetPrivate()
    return BitgetBroker(privado, StrategyParams()), privado


async def test_abrir_long_manda_compra_no_reduce_only_con_el_client_oid():
    broker, fake = _broker()
    await broker.abrir(symbol="BTCUSDT", direction=Direction.LONG, notional=400.0,
                       precio_mercado=100.0, ts=MIN, client_oid="oid-abrir-1")
    assert len(fake.ordenes) == 1
    orden = fake.ordenes[0]
    assert orden["lado"] == "buy"
    assert orden["reduce_only"] is False
    assert orden["client_oid"] == "oid-abrir-1"


async def test_abrir_short_manda_venta_no_reduce_only():
    broker, fake = _broker()
    await broker.abrir(symbol="BTCUSDT", direction=Direction.SHORT, notional=400.0,
                       precio_mercado=100.0, ts=MIN, client_oid="oid-abrir-2")
    assert fake.ordenes[0]["lado"] == "sell"
    assert fake.ordenes[0]["reduce_only"] is False


async def test_abrir_devuelve_precio_y_cantidad_del_fill_real_no_los_solicitados():
    fake = FakeBitgetPrivate(fill=FillOrden(precio=101.5, cantidad=3.94, comision=0.24))
    broker, _ = _broker(fake)
    orden = await broker.abrir(symbol="BTCUSDT", direction=Direction.LONG, notional=400.0,
                               precio_mercado=100.0, ts=MIN, client_oid="oid-abrir-3")
    # Solicitado: 400 / 100 = 4.0 exactos, al precio de referencia 100.0.
    # El fill real reporta otra cosa -eso es lo que debe devolver el broker-.
    assert orden.precio == pytest.approx(101.5)
    assert orden.cantidad == pytest.approx(3.94)
    assert orden.comision == pytest.approx(0.24)
    assert orden.precio != pytest.approx(100.0)
    assert orden.cantidad != pytest.approx(4.0)


async def test_abrir_rechaza_precio_de_mercado_no_positivo():
    broker, fake = _broker()
    with pytest.raises(ValueError, match="precio_mercado debe ser estrictamente positivo"):
        await broker.abrir(symbol="BTCUSDT", direction=Direction.LONG, notional=400.0,
                           precio_mercado=0.0, ts=MIN, client_oid="oid-x")
    assert fake.ordenes == []


async def test_cerrar_un_long_manda_venta_reduce_only():
    broker, fake = _broker()
    await broker.cerrar(symbol="BTCUSDT", direction=Direction.LONG, cantidad=2.0,
                        precio_mercado=110.0, ts=MIN)
    orden = fake.ordenes[0]
    assert orden["lado"] == "sell"
    assert orden["reduce_only"] is True
    assert orden["client_oid"]  # generado internamente, pero no vacío


async def test_cerrar_un_short_manda_compra_reduce_only():
    broker, fake = _broker()
    await broker.cerrar(symbol="BTCUSDT", direction=Direction.SHORT, cantidad=2.0,
                        precio_mercado=110.0, ts=MIN)
    orden = fake.ordenes[0]
    assert orden["lado"] == "buy"
    assert orden["reduce_only"] is True


async def test_cerrar_devuelve_precio_y_cantidad_del_fill_real():
    fake = FakeBitgetPrivate(fill=FillOrden(precio=108.7, cantidad=1.98, comision=0.13))
    broker, _ = _broker(fake)
    orden = await broker.cerrar(symbol="BTCUSDT", direction=Direction.LONG, cantidad=2.0,
                                precio_mercado=110.0, ts=MIN)
    assert orden.precio == pytest.approx(108.7)
    assert orden.cantidad == pytest.approx(1.98)
    assert orden.comision == pytest.approx(0.13)


async def test_colocar_stop_de_un_long_usa_lado_de_venta_y_reduce_only():
    broker, fake = _broker()
    stop_id = await broker.colocar_stop(symbol="BTCUSDT", direction=Direction.LONG,
                                        cantidad=4.0, precio_disparo=97.5,
                                        client_oid="oid-stop-1")
    assert stop_id
    stop = fake.stops_colocados[0]
    assert stop["lado"] == "sell"
    assert stop["precio_disparo"] == pytest.approx(97.5)
    assert stop["client_oid"] == "oid-stop-1"


async def test_colocar_stop_de_un_short_usa_lado_de_compra():
    broker, fake = _broker()
    await broker.colocar_stop(symbol="BTCUSDT", direction=Direction.SHORT,
                              cantidad=4.0, precio_disparo=105.0,
                              client_oid="oid-stop-2")
    assert fake.stops_colocados[0]["lado"] == "buy"


async def test_colocar_stop_rechaza_precio_de_disparo_no_positivo():
    broker, fake = _broker()
    with pytest.raises(ValueError, match="precio_disparo debe ser estrictamente positivo"):
        await broker.colocar_stop(symbol="BTCUSDT", direction=Direction.LONG,
                                  cantidad=4.0, precio_disparo=0.0,
                                  client_oid="oid-x")
    assert fake.stops_colocados == []


async def test_mover_stop_delega_en_el_cliente_y_devuelve_su_id():
    broker, fake = _broker()
    nuevo_id = await broker.mover_stop(symbol="BTCUSDT", stop_id="exchange-order-1",
                                       precio_disparo=99.0, cantidad=4.0)
    assert nuevo_id == "exchange-order-1"
    assert fake.stops_movidos[0]["precio_disparo"] == pytest.approx(99.0)


async def test_mover_stop_de_id_que_no_corresponde_a_un_stop_vivo_lanza_valueerror():
    """Si Bitget rechaza modify-tpsl-order porque el orderId no existe,
    BitgetPrivate.mover_stop deja pasar el RuntimeError; BitgetBroker debe
    traducirlo a ValueError, igual que PaperBroker."""

    class PrivadoQueRechaza(FakeBitgetPrivate):
        async def mover_stop(self, symbol, stop_id, precio_disparo, cantidad) -> str:
            raise RuntimeError(
                "Bitget devolvió code=40768 msg=order does not exist "
                "en /api/v2/mix/order/modify-tpsl-order"
            )

    broker, _ = _broker(PrivadoQueRechaza())
    with pytest.raises(ValueError):
        await broker.mover_stop(symbol="BTCUSDT", stop_id="id-que-no-existe",
                                precio_disparo=99.0, cantidad=4.0)


async def test_cancelar_stop_normal_delega_en_el_cliente():
    broker, fake = _broker()
    await broker.cancelar_stop(symbol="BTCUSDT", stop_id="exchange-order-1")
    assert fake.stops_cancelados == [{"symbol": "BTCUSDT", "stop_id": "exchange-order-1"}]


async def test_cancelar_stop_propaga_el_error_en_vez_de_asumir_que_ya_no_existe():
    """El `except RuntimeError` que habia aqui no protegia de nada.

    CONFIRMADO contra la cuenta de simulacion: cancelar un plan order que
    nunca existio devuelve `code=00000 success`, no un error. O sea que la
    idempotencia la da el propio Bitget, y aquel `except` -que trataba
    CUALQUIER error como "el stop ya no existe"- solo servia para disfrazar
    de rutina un fallo de autenticacion o de parametros. Ademas su alcance
    habia crecido sin querer: al leer el cuerpo de la respuesta en `_pedir`,
    los errores de negocio con HTTP 4xx pasaron de `HTTPStatusError` a
    `RuntimeError`, que es justo lo que aqui se capturaba.

    SIN VERIFICAR: que cancelar un stop YA EJECUTADO tambien devuelva
    success. Solo se probo con uno inexistente. Si resultara devolver error,
    el sintoma seria una traza en el log en cada salida por stop -ruidosa,
    no peligrosa- y entonces habria que estrechar la excepcion a ESE codigo
    concreto, que es lo que no se podia hacer mientras no se conociera."""
    fake = FakeBitgetPrivate(cancelar_lanza=True)
    broker, _ = _broker(fake)

    with pytest.raises(RuntimeError):
        await broker.cancelar_stop(symbol="BTCUSDT", stop_id="ya-no-existe")

    assert fake.stops_cancelados == [{"symbol": "BTCUSDT", "stop_id": "ya-no-existe"}]


async def test_el_runner_aisla_el_fallo_de_cancelar_para_que_propagar_sea_seguro():
    """El contrato que hace seguro propagar: quien decide que un fallo al
    cancelar no bloquea el cierre es el RUNNER, no el broker. La posicion ya
    esta cerrada de verdad -el dinero liquidado-, asi que dejarla
    `abierta = 1` por esto la atascaria para siempre ocupando un hueco de
    concurrencia. Si alguien quita ese aislamiento, propagar desde aqui deja
    de ser seguro y este test lo dice."""
    import inspect

    from scanner_volumen.bot.runner import BotRunner

    fuente = inspect.getsource(BotRunner._cancelar_stop)
    assert "try:" in fuente and "except Exception" in fuente
