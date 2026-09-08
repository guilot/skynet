"""Reconciliación al arrancar (Task 8): el bot en modo real no puede fiarse
solo de su base de datos -Bitget es quien de verdad tiene el dinero-. Estos
tests cubren las tres situaciones de la tabla del brief más el caso de
idempotencia por `client_oid` y el aislamiento de fallos.
"""
import logging

import pytest

from scanner_volumen.bot.broker import PaperBroker
from scanner_volumen.bot.model import OrdenEjecutada, PosicionExchange
from scanner_volumen.bot.portfolio import LivePortfolio
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.bot.runner import BotRunner
from scanner_volumen.config import BotConfig
from scanner_volumen.models import Direction, State
from scanner_volumen.storage.db import open_db
from scanner_volumen.strategy.model import CandleRow, StrategyParams, TransitionRow

MIN = 60_000


def _nuevo_runner(conn):
    repo = BotRepo(conn)
    cfg = BotConfig(enabled=True, modo="paper", equity_inicial=1000.0,
                    desvio_max_entrada=0.0)
    params = StrategyParams(comision_taker=0.0)
    cartera = LivePortfolio(params, cfg, repo)
    return BotRunner(params, cfg, repo, PaperBroker(params), cartera), repo


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "scanner.db")
    BotRepo(c).set_equity_inicial("paper", 1000.0)
    yield c
    c.close()


def tr(symbol="A", ts=0, prev=State.NORMAL, new=State.WATCH, price=100.0,
       score=75.0):
    return TransitionRow(ts=ts, symbol=symbol, prev_state=prev, new_state=new,
                         price=price, direction=Direction.LONG, score=score)


def velas(inicio, precios):
    return [CandleRow(ts=inicio + i * MIN, open=p, high=p, low=p, close=p)
            for i, p in enumerate(precios)]


def _sin_historial(symbol, desde):
    return []


def _sin_velas(symbol, desde):
    return []


def _sin_precio(symbol):
    return None


async def _sin_cierre(symbol):
    """`fill_de_cierre` es async (la consulta real al exchange es de red);
    este doble no encuentra nunca el fill real de cierre de nadie."""
    return None


def _cierre_para(**fills: OrdenEjecutada):
    """Fábrica de un `fill_de_cierre` async que solo conoce los símbolos
    pasados por nombre; el resto devuelve `None`, igual que `_sin_cierre`."""
    async def _fill_de_cierre(symbol):
        return fills.get(symbol)
    return _fill_de_cierre


async def test_posicion_en_ambos_se_adopta_con_el_motor_reconstruido(conn):
    # Bot 1: entra y escala a HOT (cobra el tramo y sube el stop a BE), igual
    # que en la reconstrucción tras reinicio (Fase 2) -la reconciliación
    # reutiliza exactamente la misma maquinaria (`_reconstruir_una`).
    bot1, repo = _nuevo_runner(conn)
    await bot1.on_tick([tr()], lambda s: 100.0, ahora=0)
    await bot1.on_tick([tr(ts=MIN, prev=State.WATCH, new=State.HOT, price=110.0)],
                       lambda s: 105.0, ahora=MIN)
    assert bot1.abiertas["A"].reglas.stop_en_be is True

    # Bot 2: proceso nuevo. Bitget SÍ tiene la posición (ambos la tienen).
    bot2, _ = _nuevo_runner(conn)
    historial = [tr(), tr(ts=MIN, prev=State.WATCH, new=State.HOT, price=110.0)]
    exchange = [PosicionExchange(symbol="A", direction=Direction.LONG,
                                  size=4.0, entry_price=100.0, entry_ts=0,
                                  client_oid=None)]
    await bot2.reconciliar_con_exchange(
        exchange,
        transiciones_de=lambda s, desde: historial,
        velas_de=lambda s, desde: velas(0, [100.0, 110.0]),
        precio_de=lambda s: 110.0,
        fill_de_cierre=_sin_cierre,
        ahora=2 * MIN,
    )
    assert "A" in bot2.abiertas
    assert bot2.abiertas["A"].reglas.stop_en_be is True
    assert bot2.abiertas["A"].reglas.restante == pytest.approx(0.67)


async def test_el_bot_la_cree_abierta_bitget_no_se_cierra_con_el_fill_real(conn):
    bot1, repo = _nuevo_runner(conn)
    await bot1.on_tick([tr()], lambda s: 100.0, ahora=0)
    assert "A" in bot1.abiertas

    # Bot 2: proceso nuevo, Bitget ya no tiene la posición -se cerró
    # mientras estábamos caídos-. Se le inyecta el fill real de ese cierre.
    bot2, _ = _nuevo_runner(conn)
    fill_real = OrdenEjecutada(ts=5 * MIN, precio=90.0, cantidad=4.0, comision=0.0)
    await bot2.reconciliar_con_exchange(
        [],
        transiciones_de=_sin_historial, velas_de=_sin_velas, precio_de=_sin_precio,
        fill_de_cierre=_cierre_para(A=fill_real),
        ahora=5 * MIN,
    )
    assert "A" not in bot2.abiertas
    cerradas = repo.cerradas("paper")
    assert len(cerradas) == 1
    assert cerradas[0]["symbol"] == "A"
    # LONG, entra a 100, cierra a 90: pérdida.
    assert cerradas[0]["pnl"] == pytest.approx((90.0 - 100.0) * 4.0)
    assert repo.contadores("paper").get("posiciones cerradas en el exchange") == 1


async def test_bitget_la_tiene_el_bot_no_la_reconoce_no_se_toca_y_se_veta(conn):
    bot, repo = _nuevo_runner(conn)
    exchange = [PosicionExchange(symbol="B", direction=Direction.LONG,
                                  size=1.0, entry_price=50.0, entry_ts=0,
                                  client_oid=None)]
    await bot.reconciliar_con_exchange(
        exchange,
        transiciones_de=_sin_historial, velas_de=_sin_velas, precio_de=_sin_precio,
        fill_de_cierre=_sin_cierre,
        ahora=MIN,
    )
    assert "B" not in bot.abiertas
    assert repo.abiertas("paper") == []  # no se creó ningún registro local
    assert "B" in bot.simbolos_vetados
    assert repo.contadores("paper").get("posiciones ajenas") == 1

    # una entrada posterior para ese símbolo se descarta, sin abrir nada.
    await bot.on_tick([tr(symbol="B", ts=2 * MIN)], lambda s: 50.0, ahora=2 * MIN)
    assert "B" not in bot.abiertas
    assert repo.contadores("paper").get("simbolo vetado") == 1


async def test_reserva_con_client_oid_que_casa_se_reconoce_como_propia(conn):
    repo = BotRepo(conn)
    # Un proceso murió justo después de que Bitget aceptara la orden: la fila
    # quedó reservada (confirmada=0) con datos provisionales.
    posicion_id = repo.abrir(
        modo="paper", symbol="A", direction=Direction.LONG, entry_ts=0,
        entry_price=100.0, entry_price_senal=100.0, margin=20.0,
        notional=400.0, size=4.0, fee_entrada=0.0,
        client_oid="bot-abc123", confirmada=False,
    )

    bot, _ = _nuevo_runner(conn)
    exchange = [PosicionExchange(symbol="A", direction=Direction.LONG,
                                  size=4.0, entry_price=101.5, entry_ts=0,
                                  client_oid="bot-abc123")]
    await bot.reconciliar_con_exchange(
        exchange,
        transiciones_de=lambda s, desde: [tr()], velas_de=lambda s, desde: [],
        precio_de=lambda s: 101.5,
        fill_de_cierre=_sin_cierre,
        ahora=MIN,
    )
    fila = repo.por_client_oid("paper", "bot-abc123")
    assert fila["id"] == posicion_id
    assert fila["confirmada"] == 1
    assert fila["entry_price"] == pytest.approx(101.5)
    # se reconoce como propia -no como ajena- y se adopta como cualquier otra.
    assert "A" in bot.abiertas
    assert "A" not in bot.simbolos_vetados
    assert repo.contadores("paper").get("posiciones ajenas", 0) == 0


async def test_reserva_con_client_oid_usa_la_comision_real_del_exchange(conn):
    # Hallazgo 1 (ronda 1 de revision): confirmar por client_oid no debe
    # arrastrar la comision PROVISIONAL que `_abrir` grabo antes de mandar
    # la orden -eso infla el PnL exactamente en lo que costo abrir.
    repo = BotRepo(conn)
    posicion_id = repo.abrir(
        modo="paper", symbol="A", direction=Direction.LONG, entry_ts=0,
        entry_price=100.0, entry_price_senal=100.0, margin=20.0,
        notional=400.0, size=4.0, fee_entrada=0.0,
        client_oid="bot-con-comision", confirmada=False,
    )
    bot, _ = _nuevo_runner(conn)
    exchange = [PosicionExchange(symbol="A", direction=Direction.LONG,
                                  size=4.0, entry_price=101.5, entry_ts=0,
                                  client_oid="bot-con-comision", fee_entrada=0.242)]
    await bot.reconciliar_con_exchange(
        exchange,
        transiciones_de=lambda s, desde: [tr()], velas_de=lambda s, desde: [],
        precio_de=lambda s: 101.5,
        fill_de_cierre=_sin_cierre,
        ahora=MIN,
    )
    fila = repo.por_client_oid("paper", "bot-con-comision")
    assert fila["id"] == posicion_id
    assert fila["fee_entrada"] == pytest.approx(0.242)
    assert bot.abiertas["A"].fees_acumuladas == pytest.approx(0.242)
    assert bot.abiertas["A"].pnl_acumulado == pytest.approx(-0.242)


async def test_reserva_con_client_oid_sin_comision_avisa_y_mantiene_provisional(
    conn, caplog,
):
    # Cuando el exchange no la conserva, no se pierde en silencio: se
    # mantiene el provisional pero se deja constancia explicita de que el
    # PnL de esa posicion quedara optimista.
    repo = BotRepo(conn)
    repo.abrir(
        modo="paper", symbol="A", direction=Direction.LONG, entry_ts=0,
        entry_price=100.0, entry_price_senal=100.0, margin=20.0,
        notional=400.0, size=4.0, fee_entrada=0.15,
        client_oid="bot-sin-comision", confirmada=False,
    )
    bot, _ = _nuevo_runner(conn)
    exchange = [PosicionExchange(symbol="A", direction=Direction.LONG,
                                  size=4.0, entry_price=101.5, entry_ts=0,
                                  client_oid="bot-sin-comision")]  # sin fee_entrada
    with caplog.at_level(logging.WARNING):
        await bot.reconciliar_con_exchange(
            exchange,
            transiciones_de=lambda s, desde: [tr()], velas_de=lambda s, desde: [],
            precio_de=lambda s: 101.5,
            fill_de_cierre=_sin_cierre,
            ahora=MIN,
        )
    fila = repo.por_client_oid("paper", "bot-sin-comision")
    assert fila["fee_entrada"] == pytest.approx(0.15)  # se mantiene el provisional
    mensajes = " ".join(r.getMessage() for r in caplog.records)
    assert "comision" in mensajes.lower()
    assert "optimista" in mensajes.lower()


async def test_client_oid_sin_correlacion_certera_no_se_cierra_como_no_ejecutada(conn):
    # Hallazgo 2 (ronda 1 de revision): si alguna posicion del exchange vino
    # SIN identificador de orden, la ausencia de coincidencia no es
    # concluyente -podria ser justo esa la posicion propia-. Cerrarla como
    # "nunca se ejecuto" perderia el rastro de una posicion real, apalancada
    # y con su propio stop puesto en el exchange.
    repo = BotRepo(conn)
    repo.abrir(
        modo="paper", symbol="A", direction=Direction.LONG, entry_ts=0,
        entry_price=100.0, entry_price_senal=100.0, margin=20.0,
        notional=400.0, size=4.0, fee_entrada=0.0,
        client_oid="bot-ambiguo", confirmada=False,
    )
    bot, _ = _nuevo_runner(conn)
    # el exchange SI tiene una posicion en "A", pero sin client_oid -el
    # exchange no siempre lo conserva-: no permite concluir que
    # "bot-ambiguo" no se ejecuto, porque podria ser justo esta la posicion
    # sin identificador.
    exchange = [PosicionExchange(symbol="A", direction=Direction.LONG,
                                  size=4.0, entry_price=100.0, entry_ts=0,
                                  client_oid=None)]
    await bot.reconciliar_con_exchange(
        exchange,
        transiciones_de=_sin_historial, velas_de=_sin_velas, precio_de=_sin_precio,
        fill_de_cierre=_sin_cierre,
        ahora=MIN,
    )
    fila = repo.por_client_oid("paper", "bot-ambiguo")
    # ni confirmada ni cerrada: se deja intacta para revision manual.
    assert fila["confirmada"] == 0
    assert fila["abierta"] == 1
    assert "A" not in bot.abiertas
    # VETADO desde la ronda de revision de la Task 13 (antes se afirmaba lo
    # contrario aqui): dejar la fila intacta protege el libro contable, pero
    # por si solo no protegia el dinero -el bot no reconocia esa posicion, asi
    # que abria OTRA encima del mismo simbolo en este mismo arranque, y otra
    # en cada reinicio. Y como el stop se coloca DESPUES de confirmar, la
    # posicion real de esa reserva puede estar apalancada y sin stop.
    assert "A" in bot.simbolos_vetados
    assert repo.contadores("paper").get("reserva sin correlacionar") == 1
    # el motivo es distinto del de una reserva genuinamente no ejecutada.
    assert repo.contadores("paper").get("reserva sin ejecutar", 0) == 0
    assert repo.contadores("paper").get("posiciones ajenas", 0) == 0


async def test_reserva_sin_contrapartida_se_cierra_sin_operacion(conn):
    repo = BotRepo(conn)
    posicion_id = repo.abrir(
        modo="paper", symbol="A", direction=Direction.LONG, entry_ts=0,
        entry_price=100.0, entry_price_senal=100.0, margin=20.0,
        notional=400.0, size=4.0, fee_entrada=0.0,
        client_oid="bot-nunca-ejecuto", confirmada=False,
    )
    bot, _ = _nuevo_runner(conn)
    await bot.reconciliar_con_exchange(
        [],  # Bitget no tiene absolutamente nada de esta orden
        transiciones_de=_sin_historial, velas_de=_sin_velas, precio_de=_sin_precio,
        fill_de_cierre=_sin_cierre,
        ahora=MIN,
    )
    fila = repo.por_client_oid("paper", "bot-nunca-ejecuto")
    assert fila["confirmada"] == 0  # nunca se llegó a confirmar de verdad
    assert fila["abierta"] == 0     # pero ya no ocupa el hueco
    assert repo.abiertas("paper") == []
    assert repo.contadores("paper").get("reserva sin ejecutar") == 1


async def test_un_fallo_al_reconciliar_una_no_impide_las_demas_ni_arrancar(conn):
    bot1, repo = _nuevo_runner(conn)
    await bot1.on_tick([tr(symbol="A")], lambda s: 100.0, ahora=0)
    await bot1.on_tick([tr(symbol="C", ts=0)], lambda s: 100.0, ahora=0)
    assert set(bot1.abiertas) == {"A", "C"}

    bot2, _ = _nuevo_runner(conn)
    exchange = [PosicionExchange(symbol="A", direction=Direction.LONG,
                                  size=4.0, entry_price=100.0, entry_ts=0,
                                  client_oid=None)]
    fill_c = OrdenEjecutada(ts=MIN, precio=95.0, cantidad=4.0, comision=0.0)

    def _historial_que_revienta_para_a(symbol, desde):
        if symbol == "A":
            raise RuntimeError("boom: el histórico de A no se pudo leer")
        return []

    # No debe propagar la excepción: la reconciliación de C debe completarse
    # igualmente y el proceso debe poder seguir arrancando.
    await bot2.reconciliar_con_exchange(
        exchange,
        transiciones_de=_historial_que_revienta_para_a, velas_de=_sin_velas,
        precio_de=_sin_precio,
        fill_de_cierre=_cierre_para(C=fill_c),
        ahora=MIN,
    )

    # C (bot la cree abierta, Bitget no) se reconcilió sin problema.
    assert "C" not in bot2.abiertas
    cerradas = {f["symbol"]: f for f in repo.cerradas("paper")}
    assert "C" in cerradas

    # A falló al reconciliarse: se deja tal cual (sigue abierta en la base,
    # sin adoptar en este proceso), no aparece adoptada a medias.
    assert "A" not in bot2.abiertas
    abiertas = {f["symbol"] for f in repo.abiertas("paper")}
    assert "A" in abiertas


async def test_una_reserva_no_correlacionable_veta_el_simbolo(conn):
    """Hallazgo de la ronda de revision de la Task 13, y la unica via por la
    que el cableado podia AUMENTAR la exposicion real.

    El endpoint de posiciones de Bitget no devuelve `client_oid`, asi que
    ninguna posicion del exchange lo trae y esta rama se recorre SIEMPRE que
    hay una reserva sin confirmar -la huella de un proceso que murio entre
    mandar la orden y registrarla, plausible bajo `Restart=always`-. Dejar la
    fila intacta protege el libro contable, pero sin el veto el bot abria
    OTRA posicion encima de la real en el mismo arranque, y otra en cada
    reinicio siguiente. Y como el stop se coloca DESPUES de confirmar, esa
    posicion real puede estar apalancada y sin stop en el exchange."""
    repo = BotRepo(conn)
    repo.abrir(
        modo="paper", symbol="A", direction=Direction.LONG, entry_ts=0,
        entry_price=100.0, entry_price_senal=100.0, margin=20.0,
        notional=400.0, size=4.0, fee_entrada=0.0,
        client_oid="bot-huerfano", confirmada=False,
    )
    bot, _ = _nuevo_runner(conn)
    # como en produccion: la posicion del exchange viene SIN client_oid.
    exchange = [PosicionExchange(symbol="A", direction=Direction.LONG,
                                 size=4.0, entry_price=100.0, entry_ts=0,
                                 client_oid=None)]
    await bot.reconciliar_con_exchange(
        exchange,
        transiciones_de=_sin_historial, velas_de=_sin_velas, precio_de=_sin_precio,
        fill_de_cierre=_sin_cierre,
        ahora=MIN,
    )
    assert "A" in bot.simbolos_vetados

    # y una entrada posterior en ese simbolo NO abre una segunda posicion
    await bot.on_tick([tr(symbol="A", ts=2 * MIN)], lambda s: 100.0, ahora=2 * MIN)

    assert "A" not in bot.abiertas
    assert len(repo.abiertas("paper")) == 1  # sigue solo la reserva original
    assert repo.contadores("paper").get("simbolo vetado") == 1
