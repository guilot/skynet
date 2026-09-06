import logging

import pytest

from scanner_volumen.bot.broker import PaperBroker, StopVivo
from scanner_volumen.bot.model import OrdenEjecutada
from scanner_volumen.bot.portfolio import LivePortfolio
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.bot.runner import BotRunner
from scanner_volumen.config import BotConfig
from scanner_volumen.models import Direction, State
from scanner_volumen.storage.db import open_db
from scanner_volumen.strategy.model import ExitReason, StrategyParams, TransitionRow

MIN = 60_000


@pytest.fixture(autouse=True)
def _sin_espera_real_entre_reintentos(monkeypatch):
    """`_colocar_stop_inicial` espera (`asyncio.sleep`) entre reintentos
    fallidos (Hallazgo 3, ronda 1 de revisión de la Task 7). Sin este parche,
    cada test que agota los reintentos sumaría segundos reales a la suite.
    El test que verifica la propia espera creciente vuelve a parchearla,
    localmente, para poder inspeccionar las llamadas."""
    async def _no_esperar(segundos):
        return None
    monkeypatch.setattr("scanner_volumen.bot.runner.asyncio.sleep", _no_esperar)


@pytest.fixture
def bot(tmp_path):
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn)
    repo.set_equity_inicial("paper", 1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    cartera = LivePortfolio(params, cfg, repo)
    runner = BotRunner(params, cfg, repo, PaperBroker(params), cartera)
    yield runner, repo
    conn.close()


def tr(symbol="A", ts=0, prev=State.NORMAL, new=State.HOT, price=100.0,
       score=75.0, direction=Direction.LONG):
    return TransitionRow(ts=ts, symbol=symbol, prev_state=prev, new_state=new,
                         price=price, direction=direction, score=score)


def precios(mapa):
    return lambda symbol: mapa.get(symbol)


async def test_una_entrada_abre_posicion_y_la_persiste(bot):
    runner, repo = bot
    await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)
    assert "A" in runner.abiertas
    filas = repo.abiertas("paper")
    assert len(filas) == 1
    assert filas[0]["symbol"] == "A"
    assert filas[0]["margin"] == pytest.approx(20.0)   # 2% de 1000
    assert filas[0]["notional"] == pytest.approx(400.0)  # 20 x 20


async def test_el_precio_de_entrada_es_el_de_mercado_no_el_de_la_senal(bot):
    runner, repo = bot
    # la senal decia 100 pero cuando el bot actua el mercado ya esta en 102
    await runner.on_tick([tr(price=100.0)], precios({"A": 102.0}), ahora=0)
    fila = repo.abiertas("paper")[0]
    assert fila["entry_price"] == pytest.approx(102.0)        # el ejecutado
    assert fila["entry_price_senal"] == pytest.approx(100.0)  # el de la senal
    # y el stop se ancla al ejecutado, no al de la senal
    assert runner.abiertas["A"].reglas.stop_price == pytest.approx(102.0 * 0.975)


async def test_un_descarte_no_abre_nada(bot):
    runner, repo = bot
    await runner.on_tick([tr(score=50.0)], precios({"A": 100.0}), ahora=0)
    assert runner.abiertas == {}
    assert repo.abiertas("paper") == []


async def test_sin_precio_no_se_entra(bot):
    runner, _ = bot
    await runner.on_tick([tr()], precios({}), ahora=0)
    assert runner.abiertas == {}


async def test_el_stop_cierra_la_posicion_y_registra_el_fill(bot):
    runner, repo = bot
    await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)
    # cae por debajo del stop (100 * 0.975 = 97.5)
    await runner.on_tick([], precios({"A": 97.0}), ahora=MIN)
    assert runner.abiertas == {}
    cerradas = repo.cerradas("paper")
    assert len(cerradas) == 1
    # size = notional/entry = (20 x 20)/100 = 4.0; caida de 3.0 -> pnl = -12.0
    assert cerradas[0]["pnl"] == pytest.approx(-12.0)
    fills = repo.fills_de(cerradas[0]["id"])
    assert [f["reason"] for f in fills] == [ExitReason.STOP.value]
    # con vela sintetica no hay mechas: se rellena al precio observado
    assert fills[0]["precio"] == pytest.approx(97.0)


async def test_una_transicion_a_signal_escala_dos_tramos(bot):
    runner, repo = bot
    await runner.on_tick([tr(new=State.WATCH)], precios({"A": 100.0}), ahora=0)
    await runner.on_tick([tr(ts=MIN, prev=State.WATCH, new=State.SIGNAL, price=120.0)],
                         precios({"A": 120.0}), ahora=MIN)
    pos = runner.abiertas["A"]
    assert pos.reglas.restante == pytest.approx(0.34)
    fills = repo.fills_de(pos.id)
    assert [f["reason"] for f in fills] == [
        ExitReason.SCALE_HOT.value, ExitReason.SCALE_SIGNAL.value]


async def test_el_tope_de_concurrencia_se_respeta(bot):
    runner, repo = bot
    simbolos = ["A", "B", "C", "D", "E", "F"]
    mapa = {s: 100.0 for s in simbolos}
    await runner.on_tick([tr(symbol=s) for s in simbolos], precios(mapa), ahora=0)
    assert len(runner.abiertas) == 5
    assert runner.portfolio.descartes["tope concurrencia"] == 1


async def test_el_equity_se_compone_tras_un_cierre(bot):
    runner, repo = bot
    await runner.on_tick([tr(new=State.WATCH)], precios({"A": 100.0}), ahora=0)
    # salta directo a EXTREME: cierra entero con ganancia
    await runner.on_tick(
        [tr(ts=MIN, prev=State.WATCH, new=State.EXTREME, price=130.0)],
        precios({"A": 130.0}), ahora=MIN)
    # extreme_run_min = 3 por defecto: aun corre. Se cierra al vencer.
    await runner.on_tick([], precios({"A": 130.0}), ahora=5 * MIN)
    assert runner.abiertas == {}
    # size = notional/entry = (20 x 20)/100 = 4.0; subida de 30 -> pnl = 120.0
    assert repo.equity("paper") == pytest.approx(1120.0)


async def test_pnl_short_con_precio_a_favor_es_exacto(bot):
    runner, repo = bot
    await runner.on_tick(
        [tr(new=State.WATCH, direction=Direction.SHORT)],
        precios({"A": 100.0}), ahora=0)
    # salta directo a EXTREME con el precio mas BAJO: para un short eso es
    # beneficio, no perdida
    await runner.on_tick(
        [tr(ts=MIN, prev=State.WATCH, new=State.EXTREME, price=70.0,
            direction=Direction.SHORT)],
        precios({"A": 70.0}), ahora=MIN)
    # extreme_run_min = 3 por defecto: se cierra al vencer
    await runner.on_tick([], precios({"A": 70.0}), ahora=5 * MIN)
    assert runner.abiertas == {}
    cerradas = repo.cerradas("paper")
    assert len(cerradas) == 1
    # size = notional/entry = (20 x 20)/100 = 4.0; caida de 30 a favor del
    # short -> pnl = 4.0 x 30 = 120.0 (positivo, pese a que el precio bajo)
    assert cerradas[0]["pnl"] == pytest.approx(120.0)


class _BrokerQueFallaAlCerrar:
    """Doble de prueba: abre normal pero revienta al cerrar, como simularia
    un timeout de red o cualquier otro fallo de la Fase 3 contra el exchange.

    Delega el ciclo del stop en el `PaperBroker` interno -no es lo que este
    doble prueba- para que el nuevo cableado del stop en el runner (colocar
    al abrir, cancelar al cerrar) no le impida seguir comprobando lo que
    prueba de verdad: que un fallo al cerrar no revienta el tick."""

    def __init__(self, params: StrategyParams) -> None:
        self._interno = PaperBroker(params)

    async def abrir(self, **kwargs):
        return await self._interno.abrir(**kwargs)

    async def cerrar(self, **kwargs):
        raise RuntimeError("fallo de red simulado")

    async def colocar_stop(self, **kwargs):
        return await self._interno.colocar_stop(**kwargs)

    async def mover_stop(self, **kwargs):
        return await self._interno.mover_stop(**kwargs)

    async def cancelar_stop(self, **kwargs):
        await self._interno.cancelar_stop(**kwargs)


async def test_fallo_del_broker_al_cerrar_degrada_la_posicion_sin_reventar(tmp_path):
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn)
    repo.set_equity_inicial("paper", 1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    cartera = LivePortfolio(params, cfg, repo)
    runner = BotRunner(params, cfg, repo, _BrokerQueFallaAlCerrar(params), cartera)

    await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)
    # cae por debajo del stop (97.5): la regla emite el STOP, pero el broker
    # falla al intentar cerrarlo -> la posicion debe quedar degradada, no rota
    await runner.on_tick([], precios({"A": 97.0}), ahora=MIN)

    assert "A" in runner.abiertas  # sigue ocupando su hueco de concurrencia
    pos = runner.abiertas["A"]
    assert pos.degradada is True
    assert repo.abiertas("paper")[0]["symbol"] == "A"  # sigue abierta=1 en la BD
    # F: el flag debe quedar persistido, no solo en el objeto en RAM -si no,
    # una posición degradada desaparece del informe sin dejar rastro.
    assert repo.abiertas("paper")[0]["degradada"] == 1

    # un tick posterior no debe reventar: antes de este arreglo, PositionRules
    # lanzaba ValueError por la intencion sin confirmar en cada tick siguiente
    await runner.on_tick([], precios({"A": 90.0}), ahora=2 * MIN)
    assert runner.abiertas["A"].degradada is True

    conn.close()


async def test_una_transicion_sin_precio_de_senal_no_abre_ni_revienta(bot):
    # D: un símbolo sin vela en curso produce TransitionRow.price=None. La
    # ruta viva debe descartarla en silencio -abrir con
    # entry_price_senal=None reventaría contra el NOT NULL de la columna y
    # abortaría el resto del tick.
    runner, repo = bot
    await runner.on_tick([tr(price=None)], precios({"A": 100.0}), ahora=0)
    assert runner.abiertas == {}
    assert repo.abiertas("paper") == []


async def test_una_transicion_con_precio_no_positivo_no_abre(bot):
    runner, repo = bot
    await runner.on_tick([tr(price=0.0)], precios({"A": 100.0}), ahora=0)
    assert runner.abiertas == {}
    assert repo.abiertas("paper") == []


async def test_el_stop_persiste_el_nivel_vigente_como_precio_regla(bot):
    # B: el desvío de salida debe medirse contra el stop vigente, no contra
    # el propio precio de relleno de la vela sintética (que siempre
    # coincidiría, dando desvío cero por construcción).
    runner, repo = bot
    await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)
    pid = runner.abiertas["A"].id
    # cae por debajo del stop (100 * 0.975 = 97.5) pero el bot solo observa
    # 97.0: el desvío real es 97.5 -> 97.0.
    await runner.on_tick([], precios({"A": 97.0}), ahora=MIN)
    fills = repo.fills_de(pid)
    assert [f["reason"] for f in fills] == [ExitReason.STOP.value]
    assert fills[0]["precio"] == pytest.approx(97.0)
    assert fills[0]["precio_regla"] == pytest.approx(97.5)


async def test_el_stale_be_persiste_el_precio_de_entrada_como_precio_regla(bot):
    # STALE_BE promete no salir por debajo de break-even: el nivel de
    # referencia es `entry_price`, no el precio de la transición ni el de
    # relleno -aquí deliberadamente distinto (105) para que la aserción
    # distinga un precio_regla correcto de uno que colara el de relleno.
    runner, repo = bot
    await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)
    pid = runner.abiertas["A"].id
    # stale_min = 10 min por defecto: sin cambios de estado, se arma la
    # salida en BE. El precio ya subió a 105 (mejor que BE) al vencer.
    await runner.on_tick([], precios({"A": 105.0}), ahora=10 * MIN)
    fills = repo.fills_de(pid)
    assert [f["reason"] for f in fills] == [ExitReason.STALE_BE.value]
    assert fills[0]["precio"] == pytest.approx(105.0)
    assert fills[0]["precio_regla"] == pytest.approx(100.0)  # entry_price, no 105


async def test_los_tramos_de_escalada_persisten_el_precio_de_la_transicion(bot):
    # SCALE_HOT/SCALE_SIGNAL: el nivel de referencia es el precio de la
    # transición que disparó el tramo (`intent.precio_referencia`), no el
    # precio al que el bot de verdad ejecutó ese tramo.
    runner, repo = bot
    await runner.on_tick([tr(new=State.WATCH)], precios({"A": 100.0}), ahora=0)
    pid = runner.abiertas["A"].id
    # la señal decía 120 pero el bot solo observa 118 al actuar
    await runner.on_tick(
        [tr(ts=MIN, prev=State.WATCH, new=State.SIGNAL, price=120.0)],
        precios({"A": 118.0}), ahora=MIN)
    fills = {f["reason"]: f for f in repo.fills_de(pid)}
    assert fills[ExitReason.SCALE_HOT.value]["precio_regla"] == pytest.approx(120.0)
    assert fills[ExitReason.SCALE_HOT.value]["precio"] == pytest.approx(118.0)
    assert fills[ExitReason.SCALE_SIGNAL.value]["precio_regla"] == pytest.approx(120.0)


async def test_el_extreme_por_temporizador_no_lleva_precio_regla(bot):
    # EXTREME por temporizador cierra a mercado adrede: aquí el cero SÍ es
    # correcto, así que no hay nivel prometido que persistir.
    runner, repo = bot
    await runner.on_tick([tr(new=State.WATCH)], precios({"A": 100.0}), ahora=0)
    pid = runner.abiertas["A"].id
    await runner.on_tick(
        [tr(ts=MIN, prev=State.WATCH, new=State.EXTREME, price=130.0)],
        precios({"A": 130.0}), ahora=MIN)
    # extreme_run_min = 3 por defecto: aun corre. Se cierra al vencer.
    await runner.on_tick([], precios({"A": 130.0}), ahora=5 * MIN)
    fills = {f["reason"]: f for f in repo.fills_de(pid)}
    assert fills[ExitReason.EXTREME.value]["precio_regla"] is None


async def test_los_contadores_del_informe_se_persisten(bot):
    # A: transiciones vistas y concurrencia máxima deben sobrevivir a un
    # reinicio -viven en `bot_contadores`, no solo en atributos en RAM.
    runner, repo = bot
    simbolos = ["A", "B", "C"]
    await runner.on_tick(
        [tr(symbol=s) for s in simbolos], precios({s: 100.0 for s in simbolos}),
        ahora=0,
    )
    await runner.on_tick([], precios({s: 100.0 for s in simbolos}), ahora=MIN)
    contadores = repo.contadores("paper")
    assert contadores["transiciones"] == 3  # el segundo tick no trajo ninguna
    assert contadores["max_concurrentes"] == 3


class _BrokerParcial:
    """Cierra solo una parte de lo pedido, las veces que se le diga.

    El ciclo del stop no es lo que este doble prueba (los fills parciales al
    cerrar), así que delega colocar/mover/cancelar en un `PaperBroker`
    interno para que el nuevo cableado en el runner no le afecte."""

    def __init__(self, params, fraccion_servida=0.5, veces_parcial=99):
        self._params = params
        self._interno = PaperBroker(params)
        self.fraccion_servida = fraccion_servida
        self.veces_parcial = veces_parcial
        self.cierres = []

    async def abrir(self, *, symbol, direction, notional, precio_mercado, ts,
                    client_oid=None):
        return OrdenEjecutada(ts=ts, precio=precio_mercado,
                              cantidad=notional / precio_mercado, comision=0.0)

    async def cerrar(self, *, symbol, direction, cantidad, precio_mercado, ts):
        self.cierres.append(cantidad)
        if len(self.cierres) <= self.veces_parcial:
            servida = cantidad * self.fraccion_servida
        else:
            servida = cantidad
        return OrdenEjecutada(ts=ts, precio=precio_mercado, cantidad=servida,
                              comision=0.0)

    async def colocar_stop(self, **kwargs):
        return await self._interno.colocar_stop(**kwargs)

    async def mover_stop(self, **kwargs):
        return await self._interno.mover_stop(**kwargs)

    async def cancelar_stop(self, **kwargs):
        await self._interno.cancelar_stop(**kwargs)


async def test_un_fill_parcial_se_reintenta_hasta_completarse(tmp_path):
    # el broker sirve la mitad la primera vez y todo la segunda: el bot debe
    # reintentar el resto y acabar contabilizando la cantidad COMPLETA
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn); repo.set_equity_inicial("paper", 1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    broker = _BrokerParcial(params, fraccion_servida=0.5, veces_parcial=1)
    runner = BotRunner(params, cfg, repo, broker, LivePortfolio(params, cfg, repo))

    await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)
    await runner.on_tick([], precios({"A": 97.0}), ahora=MIN)   # stop

    assert len(broker.cierres) >= 2, "no reintento el resto"
    assert runner.abiertas == {}
    cerradas = repo.cerradas("paper")
    assert len(cerradas) == 1
    # 4 unidades a 100, cerradas a 97: -12.0 exactos si se conto TODO
    assert cerradas[0]["pnl"] == pytest.approx(-12.0)
    conn.close()


async def test_si_el_resto_no_se_completa_la_posicion_queda_degradada(tmp_path):
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn); repo.set_equity_inicial("paper", 1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    broker = _BrokerParcial(params, fraccion_servida=0.5)  # siempre parcial
    runner = BotRunner(params, cfg, repo, broker, LivePortfolio(params, cfg, repo))

    await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)
    await runner.on_tick([], precios({"A": 97.0}), ahora=MIN)

    # no se descuadra el motor en silencio: se marca y se deja para el arranque
    assert "A" in runner.abiertas
    assert runner.abiertas["A"].degradada is True
    conn.close()


async def test_la_fila_se_reserva_antes_de_mandar_la_orden(tmp_path):
    """Si el proceso muere entre la orden y el registro, la posicion queda
    huerfana en el exchange. Reservando la fila ANTES, con su client_oid, la
    reconciliacion puede reconocerla como propia."""
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn); repo.set_equity_inicial("paper", 1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    vistas = {}

    class _BrokerQueMiraLaBase:
        async def abrir(self, *, symbol, direction, notional, precio_mercado,
                        ts, client_oid):
            # en el momento de mandar la orden, la fila ya tiene que existir
            vistas["fila"] = repo.por_client_oid("paper", client_oid)
            return OrdenEjecutada(ts=ts, precio=precio_mercado,
                                  cantidad=notional / precio_mercado, comision=0.0)

        async def cerrar(self, **kw):
            raise AssertionError("no deberia cerrarse")

        async def colocar_stop(self, **kw):
            # no es lo que este test comprueba: basta con no reventar
            return "stop-fake"

        async def mover_stop(self, **kw):
            return "stop-fake"

        async def cancelar_stop(self, **kw):
            return None

    runner = BotRunner(params, cfg, repo, _BrokerQueMiraLaBase(),
                       LivePortfolio(params, cfg, repo))
    await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)

    assert vistas["fila"] is not None, "la fila no existia al mandar la orden"
    assert vistas["fila"]["symbol"] == "A"
    conn.close()


# --- Task 7: el ciclo del stop en el exchange ---

class _BrokerConRegistroDeStops:
    """Envuelve un `PaperBroker` real -para que el ciclo de vida del stop se
    comporte como en producción, incluida la validación de `mover_stop`- pero
    además anota cada llamada, que es lo que estos tests necesitan verificar."""

    def __init__(self, params: StrategyParams) -> None:
        self._interno = PaperBroker(params)
        self.stops_colocados: list[StopVivo] = []
        self.stops_movidos: list[StopVivo] = []
        self.stops_cancelados: list[str] = []

    async def abrir(self, **kwargs):
        return await self._interno.abrir(**kwargs)

    async def cerrar(self, **kwargs):
        return await self._interno.cerrar(**kwargs)

    async def colocar_stop(self, *, symbol, direction, cantidad, precio_disparo,
                           client_oid):
        stop_id = await self._interno.colocar_stop(
            symbol=symbol, direction=direction, cantidad=cantidad,
            precio_disparo=precio_disparo, client_oid=client_oid,
        )
        self.stops_colocados.append(StopVivo(
            stop_id=stop_id, symbol=symbol, precio_disparo=precio_disparo,
            cantidad=cantidad,
        ))
        return stop_id

    async def mover_stop(self, *, symbol, stop_id, precio_disparo):
        nuevo_id = await self._interno.mover_stop(
            symbol=symbol, stop_id=stop_id, precio_disparo=precio_disparo,
        )
        self.stops_movidos.append(StopVivo(
            stop_id=nuevo_id, symbol=symbol, precio_disparo=precio_disparo,
            cantidad=0.0,
        ))
        return nuevo_id

    async def cancelar_stop(self, *, symbol, stop_id):
        await self._interno.cancelar_stop(symbol=symbol, stop_id=stop_id)
        self.stops_cancelados.append(stop_id)


def _runner_con_registro_de_stops(repo, cfg, params):
    broker = _BrokerConRegistroDeStops(params)
    runner = BotRunner(params, cfg, repo, broker, LivePortfolio(params, cfg, repo))
    return runner, broker


async def test_al_abrir_se_coloca_un_stop_en_el_nivel_de_la_regla(tmp_path):
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn); repo.set_equity_inicial("paper", 1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    runner, broker = _runner_con_registro_de_stops(repo, cfg, params)

    # entrada LONG a 100 -> stop en 97.5 (stop_pct = 0.025 por defecto)
    await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)

    assert broker.stops_colocados[-1].precio_disparo == pytest.approx(97.5)
    pos = runner.abiertas["A"]
    assert pos.stop_id == broker.stops_colocados[-1].stop_id
    # y queda persistido, no solo en el objeto en RAM
    assert repo.abiertas("paper")[0]["stop_id"] == pos.stop_id
    conn.close()


async def test_una_parcial_en_beneficio_mueve_el_stop_a_break_even(tmp_path):
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn); repo.set_equity_inicial("paper", 1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    runner, broker = _runner_con_registro_de_stops(repo, cfg, params)

    await runner.on_tick([tr(new=State.WATCH)], precios({"A": 100.0}), ahora=0)
    stop_inicial_id = broker.stops_colocados[-1].stop_id
    # tramo HOT en beneficio (110 > 100 de entrada): la regla sube el stop a
    # break-even (reglas.stop_en_be pasa a True) y el del exchange debe
    # moverse al precio de entrada
    await runner.on_tick(
        [tr(ts=MIN, prev=State.WATCH, new=State.HOT, price=110.0)],
        precios({"A": 110.0}), ahora=MIN)

    pos = runner.abiertas["A"]
    assert pos.reglas.stop_en_be is True
    assert broker.stops_movidos[-1].precio_disparo == pytest.approx(100.0)
    # el stop_id cambia -mover es cancelar y colocar de nuevo- y el nuevo
    # valor queda persistido
    assert pos.stop_id == broker.stops_movidos[-1].stop_id
    assert pos.stop_id != stop_inicial_id
    assert repo.abiertas("paper")[0]["stop_id"] == pos.stop_id
    conn.close()


async def test_al_cerrar_se_cancela_el_stop(tmp_path):
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn); repo.set_equity_inicial("paper", 1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    runner, broker = _runner_con_registro_de_stops(repo, cfg, params)

    await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)
    sid = broker.stops_colocados[-1].stop_id
    # cae por debajo del stop (97.5): la regla cierra la posicion entera
    await runner.on_tick([], precios({"A": 97.0}), ahora=MIN)

    assert runner.abiertas == {}
    assert broker.stops_cancelados == [sid]
    conn.close()


class _BrokerQueFallaAlColocarStop:
    """Nunca consigue colocar el stop: simula que el exchange rechaza la
    orden o que la red falla las tres veces. `mover_stop`/`cancelar_stop` no
    deberian llegar a llamarse -el stop nunca llego a existir-."""

    def __init__(self, params: StrategyParams) -> None:
        self._interno = PaperBroker(params)
        self.intentos_colocar = 0

    async def abrir(self, **kwargs):
        return await self._interno.abrir(**kwargs)

    async def cerrar(self, **kwargs):
        return await self._interno.cerrar(**kwargs)

    async def colocar_stop(self, **kwargs):
        self.intentos_colocar += 1
        raise RuntimeError("fallo simulado al colocar el stop")

    async def mover_stop(self, **kwargs):
        raise AssertionError("no deberia llamarse: el stop nunca se coloco")

    async def cancelar_stop(self, **kwargs):
        raise AssertionError("no deberia llamarse: el stop nunca se coloco")


async def test_si_no_se_puede_colocar_el_stop_se_cierra_la_posicion(tmp_path):
    # una posicion apalancada sin red es peor que una perdida pequena
    # realizada: si colocar el stop falla incluso tras reintentar, la
    # posicion se cierra a mercado en el acto.
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn); repo.set_equity_inicial("paper", 1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    broker = _BrokerQueFallaAlColocarStop(params)
    runner = BotRunner(params, cfg, repo, broker, LivePortfolio(params, cfg, repo))

    await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)

    assert runner.abiertas == {}
    cerradas = repo.cerradas("paper")
    assert len(cerradas) == 1
    assert cerradas[0]["symbol"] == "A"
    conn.close()


async def test_colocar_stop_se_reintenta_dos_veces_antes_de_cerrar(tmp_path):
    # documenta la politica de reintentos: intento inicial + 2 reintentos =
    # 3 intentos en total antes de rendirse.
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn); repo.set_equity_inicial("paper", 1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    broker = _BrokerQueFallaAlColocarStop(params)
    runner = BotRunner(params, cfg, repo, broker, LivePortfolio(params, cfg, repo))

    await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)

    assert broker.intentos_colocar == 3


async def test_colocar_stop_espera_creciente_entre_reintentos(tmp_path, monkeypatch):
    # Hallazgo 3 (ronda 1 de revision): tres intentos seguidos, sin espera,
    # fallarian los tres por la misma causa contra un exchange rate-limitado
    # o momentaneamente caido. Se parchea `asyncio.sleep` para registrar las
    # llamadas en vez de esperar de verdad -esta prueba SI quiere inspeccionar
    # la propia espera, a diferencia del resto de tests de este fichero, que
    # la desactivan via el fixture `_sin_espera_real_entre_reintentos`.
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn); repo.set_equity_inicial("paper", 1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    broker = _BrokerQueFallaAlColocarStop(params)
    runner = BotRunner(params, cfg, repo, broker, LivePortfolio(params, cfg, repo))

    esperas = []

    async def _espera_registrada(segundos):
        esperas.append(segundos)

    monkeypatch.setattr("scanner_volumen.bot.runner.asyncio.sleep", _espera_registrada)

    await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)

    # dos esperas entre los 3 intentos -no se espera tras el ultimo, que ya
    # decide cerrar a mercado-, creciendo con el numero de intento
    assert len(esperas) == 2
    assert 0 < esperas[0] < esperas[1]
    conn.close()


class _BrokerQueFallaAlMoverStop:
    """Coloca el stop sin problema pero revienta al intentar moverlo -como
    simularia que el stop ya salto en el exchange justo antes de este tick,
    o un fallo de red al llamar a `mover_stop`."""

    def __init__(self, params: StrategyParams) -> None:
        self._interno = PaperBroker(params)

    async def abrir(self, **kwargs):
        return await self._interno.abrir(**kwargs)

    async def cerrar(self, **kwargs):
        return await self._interno.cerrar(**kwargs)

    async def colocar_stop(self, **kwargs):
        return await self._interno.colocar_stop(**kwargs)

    async def mover_stop(self, **kwargs):
        raise ValueError("stop_id ya no corresponde a un stop vivo (simulado)")

    async def cancelar_stop(self, **kwargs):
        await self._interno.cancelar_stop(**kwargs)


async def test_fallo_al_mover_el_stop_no_tumba_el_tick(tmp_path):
    # Hallazgo 2 (ronda 1 de revision): `mover_stop` SI lanza cuando el
    # `stop_id` ya no corresponde a un stop vivo (a diferencia de
    # `cancelar_stop`, que es idempotente). No debe tumbar el tick ni
    # degradar la posicion -el stop local sigue vigilando.
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn); repo.set_equity_inicial("paper", 1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    broker = _BrokerQueFallaAlMoverStop(params)
    runner = BotRunner(params, cfg, repo, broker, LivePortfolio(params, cfg, repo))

    await runner.on_tick([tr(new=State.WATCH)], precios({"A": 100.0}), ahora=0)
    # tramo HOT en beneficio: la regla sube el stop a break-even e intenta
    # moverlo en el exchange, pero el broker revienta.
    await runner.on_tick(
        [tr(ts=MIN, prev=State.WATCH, new=State.HOT, price=110.0)],
        precios({"A": 110.0}), ahora=MIN)

    pos = runner.abiertas["A"]
    assert pos.reglas.stop_en_be is True
    assert pos.degradada is False
    # el tick siguiente tampoco revienta ni degrada la posicion
    await runner.on_tick([], precios({"A": 105.0}), ahora=2 * MIN)
    assert runner.abiertas["A"].degradada is False
    conn.close()


class _BrokerQueFallaAlCancelarStop:
    """Abre y cierra con normalidad, pero revienta al cancelar el stop -un
    fallo real de red o del exchange, no el camino idempotente normal en el
    que el stop ya no existe."""

    def __init__(self, params: StrategyParams) -> None:
        self._interno = PaperBroker(params)

    async def abrir(self, **kwargs):
        return await self._interno.abrir(**kwargs)

    async def cerrar(self, **kwargs):
        return await self._interno.cerrar(**kwargs)

    async def colocar_stop(self, **kwargs):
        return await self._interno.colocar_stop(**kwargs)

    async def mover_stop(self, **kwargs):
        return await self._interno.mover_stop(**kwargs)

    async def cancelar_stop(self, **kwargs):
        raise RuntimeError("fallo de red simulado al cancelar")


async def test_fallo_al_cancelar_el_stop_no_impide_cerrar(tmp_path):
    # Hallazgo 2 (ronda 1 de revision): si cancelar el stop falla de verdad
    # (no el camino idempotente normal), la posicion debe darse por cerrada
    # igual -esta cerrada de verdad, con el dinero ya liquidado-, no quedar
    # `abierta = 1` para siempre por esto.
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn); repo.set_equity_inicial("paper", 1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    broker = _BrokerQueFallaAlCancelarStop(params)
    runner = BotRunner(params, cfg, repo, broker, LivePortfolio(params, cfg, repo))

    await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)
    # cae por debajo del stop: se cierra, pero cancelar el stop en el
    # exchange falla -no debe impedir dar la posicion por cerrada.
    await runner.on_tick([], precios({"A": 97.0}), ahora=MIN)

    assert runner.abiertas == {}
    cerradas = repo.cerradas("paper")
    assert len(cerradas) == 1
    assert cerradas[0]["pnl"] == pytest.approx(-12.0)
    conn.close()


class _BrokerQueFallaAlColocarYAlCerrar:
    """Nunca coloca el stop, y cuando el runner intenta el cierre de
    emergencia a mercado que deberia sustituirlo, tambien falla -la red no
    daba para nada."""

    def __init__(self, params: StrategyParams) -> None:
        self.intentos_colocar = 0

    async def abrir(self, *, symbol, direction, notional, precio_mercado, ts,
                    client_oid):
        return OrdenEjecutada(ts=ts, precio=precio_mercado,
                              cantidad=notional / precio_mercado, comision=0.0)

    async def cerrar(self, **kwargs):
        raise RuntimeError("fallo de red simulado al cerrar de emergencia")

    async def colocar_stop(self, **kwargs):
        self.intentos_colocar += 1
        raise RuntimeError("fallo simulado al colocar el stop")

    async def mover_stop(self, **kwargs):
        raise AssertionError("no deberia llamarse: el stop nunca se coloco")

    async def cancelar_stop(self, **kwargs):
        raise AssertionError("no deberia llamarse: el stop nunca se coloco")


async def test_si_falla_colocar_y_tambien_el_cierre_de_emergencia_queda_degradada_y_abierta(
    tmp_path, caplog,
):
    # Hallazgo 1 (ronda 1 de revision): si el cierre de emergencia TAMBIEN
    # falla, la posicion no debe desaparecer ni registrarse como "entrada
    # descartada" -sigue realmente abierta, apalancada y sin stop, y eso
    # tiene que quedar clarisimo en el log para que alguien lo mire a mano.
    conn = open_db(tmp_path / "scanner.db")
    repo = BotRepo(conn); repo.set_equity_inicial("paper", 1000.0)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    broker = _BrokerQueFallaAlColocarYAlCerrar(params)
    runner = BotRunner(params, cfg, repo, broker, LivePortfolio(params, cfg, repo))

    with caplog.at_level(logging.ERROR):
        await runner.on_tick([tr()], precios({"A": 100.0}), ahora=0)

    assert broker.intentos_colocar == 3
    # sigue abierta -ni se descarto ni desaparecio- y marcada degradada,
    # persistido en la base
    assert "A" in runner.abiertas
    pos = runner.abiertas["A"]
    assert pos.degradada is True
    assert pos.stop_id is None
    fila = repo.abiertas("paper")[0]
    assert fila["symbol"] == "A"
    assert fila["degradada"] == 1
    # el log dice explicitamente que hace falta mirarlo a mano, no que se
    # "descarto" la entrada
    mensajes = " ".join(r.getMessage() for r in caplog.records)
    assert "intervencion manual" in mensajes.lower()
    assert "se descarta esta entrada" not in mensajes
    conn.close()
