# tests/app/test_orchestrator.py
import asyncio
import logging

import pytest

from scanner_volumen.app.orchestrator import Orchestrator
from scanner_volumen.bitget.ws import WsEvent
from scanner_volumen.config import load_config
from scanner_volumen.engine.profile import build_profile
from scanner_volumen.models import Candle, State, Ticker
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import (
    CandleRepo, ProfileRepo, SignalRepo, SupplyRepo,
)
from scanner_volumen.universe.selector import UniverseUpdate
from pathlib import Path

MINUTO = 60_000
DIA = 1440 * MINUTO


def vela(ts, close=100.0, vol=100.0):
    return Candle(ts=ts, open=close, high=close, low=close, close=close,
                  base_vol=vol / close, quote_vol=vol)


class SupplyFalso:
    def market_cap(self, symbol):
        return 8e7

    async def refresh(self, symbols, now_ms):
        return 0


class BootstrapperFalso:
    def __init__(self, cfg):
        self._cfg = cfg
        self.pedidos = []

    async def bootstrap_symbol(self, symbol, now_ms):
        self.pedidos.append(symbol)
        velas = [vela(d * DIA + m * MINUTO, vol=100.0)
                 for d in range(14) for m in range(1440)]
        return build_profile(symbol, velas, self._cfg)

    def expect(self, symbols):
        pass

    def progress(self):
        return (len(self.pedidos), len(self.pedidos))

    def mark_loaded(self, symbol):
        pass


class WsFalso:
    """Registra suscripciones sin abrir ningún socket real."""

    def __init__(self):
        self.subscribed: list[list[str]] = []
        self.unsubscribed: list[list[str]] = []

    async def subscribe(self, symbols):
        self.subscribed.append(list(symbols))

    async def unsubscribe(self, symbols):
        self.unsubscribed.append(list(symbols))


class BootstrapperLento:
    """Bootstrapper cuyo bootstrap_symbol no termina hasta que el test lo
    libera explícitamente, para poder comprobar que apply_universe no
    espera a que termine."""

    def __init__(self, cfg):
        self._cfg = cfg
        self.terminados: list[str] = []
        self.evento = asyncio.Event()

    async def bootstrap_symbol(self, symbol, now_ms):
        await self.evento.wait()
        self.terminados.append(symbol)
        velas = [vela(d * DIA + m * MINUTO, vol=100.0)
                 for d in range(14) for m in range(1440)]
        return build_profile(symbol, velas, self._cfg)

    def expect(self, symbols):
        pass

    def progress(self):
        return (len(self.terminados), len(self.terminados))

    def mark_loaded(self, symbol):
        pass


@pytest.fixture
def orq(tmp_path):
    cfg = load_config(Path("config.toml"))
    conn = open_db(tmp_path / "t.db")
    o = Orchestrator(
        cfg=cfg,
        rest=None,
        ws=None,
        candle_repo=CandleRepo(conn),
        profile_repo=ProfileRepo(conn),
        signal_repo=SignalRepo(conn),
        supply=SupplyFalso(),
        bootstrapper=BootstrapperFalso(cfg.profile),
    )
    yield o
    conn.close()


async def test_una_vela_del_ws_llega_al_buffer(orq):
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(14 * DIA)])
    )
    assert orq.state.snapshot("AAAUSDT") is None  # aún no evaluado
    assert "AAAUSDT" in orq.dirty


async def test_el_snapshot_aparece_tras_evaluar(orq):
    await orq.ensure_profile("AAAUSDT", now_ms=14 * DIA)
    orq.set_ticker(Ticker("AAAUSDT", 100.0, 6.4, 5e6, 100.0, 0.0001, 0))
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(14 * DIA)])
    )
    orq.evaluate(now_ms=14 * DIA + 30_000)
    snap = orq.state.snapshot("AAAUSDT")
    assert snap is not None
    assert snap.breakdown is not None


async def test_evaluate_solo_procesa_los_simbolos_marcados(orq):
    await orq.ensure_profile("AAAUSDT", now_ms=14 * DIA)
    orq.set_ticker(Ticker("AAAUSDT", 100.0, 6.4, 5e6, 100.0, 0.0001, 0))
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(14 * DIA)])
    )
    orq.evaluate(now_ms=14 * DIA + 30_000)
    assert orq.dirty == set()
    orq.evaluate(now_ms=14 * DIA + 31_000)  # nada marcado: no hace trabajo
    assert orq.state.snapshot("AAAUSDT").updated_ms == 14 * DIA + 30_000


async def test_un_pump_genera_transicion_y_se_persiste(orq):
    # Diseño: una señal se persiste al entrar en HOT (o superior) y de nuevo en
    # cada escalado posterior, nunca en cada tick. El calentamiento plano no
    # debe generar ninguna fila; el pump debe generar exactamente una por cada
    # escalado que cruce HOT o más arriba (ni más -> tick spam, ni menos ->
    # señal perdida).
    base = 14 * DIA
    await orq.ensure_profile("AAAUSDT", now_ms=base)
    orq.set_ticker(Ticker("AAAUSDT", 130.0, 14.0, 5e6, 100.0, 0.0001, 0))

    for m in range(0, 120):
        await orq.handle_ws_event(
            WsEvent(kind="update", symbol="AAAUSDT",
                    candles=[vela(base + m * MINUTO, close=100.0, vol=100.0)])
        )
        orq.evaluate(now_ms=base + m * MINUTO + 59_000)

    assert orq.signal_repo.recent(since_ms=0) == []  # calentamiento plano: nada persistido

    escaladas_a_hot_o_mas = 0
    for i, m in enumerate(range(120, 128)):
        await orq.handle_ws_event(
            WsEvent(kind="update", symbol="AAAUSDT",
                    candles=[vela(base + m * MINUTO, close=100.0 + i * 3, vol=1200.0)])
        )
        transiciones = orq.evaluate(now_ms=base + m * MINUTO + 59_000)
        escaladas_a_hot_o_mas += sum(
            1 for t in transiciones
            if t.escalated and t.current.rank >= State.HOT.rank
        )

    snap = orq.state.snapshot("AAAUSDT")
    assert snap.breakdown.total > 50
    assert snap.state.rank >= State.WATCH.rank
    assert escaladas_a_hot_o_mas >= 1
    assert len(orq.signal_repo.recent(since_ms=0)) == escaladas_a_hot_o_mas


async def test_rvol_session_y_vwap_reflejan_la_sesion_completa_no_solo_lo_llegado_por_ws(orq):
    """Regresión C3: sin sembrar el buffer desde SQLite en el arranque,
    rvol_session y vwap solo ven lo que llega por WebSocket desde que
    arrancó el proceso, no la sesión completa desde las 00:00 UTC."""
    base = 14 * DIA
    minutos_del_dia = 14 * 60  # el proceso "arranca" a las 14:00 UTC
    velas_dia = [
        vela(base + m * MINUTO, close=100.0 + m * 0.01, vol=100.0)
        for m in range(minutos_del_dia)
    ]
    orq.candle_repo.save_many("AAAUSDT", velas_dia)

    await orq.ensure_profile("AAAUSDT", now_ms=base + minutos_del_dia * MINUTO)
    orq.set_ticker(Ticker("AAAUSDT", 108.4, 1.0, 5e6, 100.0, 0.0001, 0))
    # el WS solo entrega la vela del minuto en curso, como en un arranque real:
    # el histórico ya guardado en SQLite nunca llega por WS.
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT",
                candles=[vela(base + minutos_del_dia * MINUTO, close=108.4, vol=100.0)])
    )
    orq.evaluate(now_ms=base + minutos_del_dia * MINUTO + 30_000)

    snap = orq.state.snapshot("AAAUSDT")
    # sesión completa (841 velas a 100 quote-vol constante) contra un
    # baseline acumulado idéntico: rvol_session debe rondar 1.0, no
    # 100/84100 (~0.0012), que es lo que da una sola vela de WS.
    assert snap.metrics.rvol_session == pytest.approx(1.0, rel=1e-6)
    # vwap de la sesión completa (closes de 100.0 a 108.4) es su promedio,
    # 104.2; sin siembra sería 108.4, el close de la única vela de WS.
    assert snap.metrics.vwap == pytest.approx(104.2, rel=1e-6)


async def test_demand_burst_es_alcanzable_a_la_cadencia_de_produccion(orq):
    """Regresión C2 (Task 2): alimenta el orquestador exactamente como en
    producción -- un tick de evaluate() por segundo y una actualización de WS
    en (casi) cada tick, como hacen el WS real (~cada 2.5s) y poll_tickers
    (~cada 3s) -- durante más de 5 minutos simulados, y comprueba que
    demand_burst deja de ser None.

    Antes del fix, record_rvol se llamaba desde evaluate() estampando con
    now_ms (el reloj del tick) en un deque(maxlen=120): a un tick por segundo
    y sucio en casi todos los ticks, el deque solo cubre ~120s de pared, muy
    por debajo de los ~300s que pide _rvol_hace(BURST_LOOKBACK_MIN=5), así
    que la muestra de referencia nunca estaba en el historial y demand_burst
    quedaba en None para siempre.
    """
    base = 14 * DIA
    await orq.ensure_profile("AAAUSDT", now_ms=base)
    orq.set_ticker(Ticker("AAAUSDT", 100.0, 6.4, 5e6, 100.0, 0.0001, 0))

    for segundo in range(0, 6 * 60 + 1):  # > 5 minutos simulados, tick de 1s
        ahora = base + segundo * 1000
        minuto = segundo // 60
        # el WS actualiza la vela en curso del minuto actual en cada tick,
        # como en producción (símbolo sucio casi siempre); el volumen sube
        # para que rvol_1m_closed nunca sea None ni constante.
        await orq.handle_ws_event(
            WsEvent(kind="update", symbol="AAAUSDT",
                    candles=[vela(base + minuto * MINUTO, vol=100.0 + minuto)])
        )
        orq.evaluate(now_ms=ahora)

    snap = orq.state.snapshot("AAAUSDT")
    assert snap is not None
    assert snap.metrics.demand_burst is not None


async def test_ranked_ordena_por_score_descendente(orq):
    base = 14 * DIA
    datos = {
        "AAAUSDT": (100.0, 100.0, Ticker("AAAUSDT", 100.0, 1.0, 5e6, 100.0, 0.0001, 0)),
        "BBBUSDT": (130.0, 1200.0, Ticker("BBBUSDT", 130.0, 14.0, 5e6, 100.0, 0.0001, 0)),
    }
    for sym, (close, vol, ticker) in datos.items():
        await orq.ensure_profile(sym, now_ms=base)
        orq.set_ticker(ticker)
        await orq.handle_ws_event(
            WsEvent(kind="update", symbol=sym, candles=[vela(base, close=close, vol=vol)])
        )
    orq.evaluate(now_ms=base + 30_000)
    ranking = orq.state.ranked()
    assert len(ranking) == 2
    assert ranking[0].symbol == "BBBUSDT"
    assert ranking[0].breakdown.total > ranking[1].breakdown.total


async def test_un_snapshot_del_ws_carga_varias_velas(orq):
    await orq.ensure_profile("AAAUSDT", now_ms=14 * DIA)
    velas = [vela(14 * DIA + m * MINUTO) for m in range(10)]
    await orq.handle_ws_event(
        WsEvent(kind="snapshot", symbol="AAAUSDT", candles=velas)
    )

    buffer = orq.buffers["AAAUSDT"]
    # de las 10 velas del snapshot, las primeras 9 quedan cerradas (y
    # persistidas); la última sigue en curso y NO debe aparecer como cerrada
    # ni escribirse como si lo estuviera.
    assert len(buffer.all_closed()) == 9
    assert buffer.current().ts == velas[-1].ts
    assert orq.candle_repo.latest_ts("AAAUSDT") == velas[-2].ts

    orq.set_ticker(Ticker("AAAUSDT", 100.0, 1.0, 5e6, 100.0, 0.0001, 0))
    orq.evaluate(now_ms=14 * DIA + 10 * MINUTO)
    assert orq.state.snapshot("AAAUSDT") is not None


async def test_un_evento_de_error_no_rompe_el_orquestador(orq):
    await orq.handle_ws_event(WsEvent(kind="error", symbol=None, candles=[]))
    assert orq.dirty == set()


class RestFalsoParaHuecos:
    """Simula get_history_candles: `limit` velas terminando en `end_time_ms`,
    exactamente como el REST real (que solo pagina hacia atrás con endTime)."""

    def __init__(self):
        self.llamadas = []

    async def get_history_candles(self, symbol, end_time_ms, limit=200):
        self.llamadas.append((symbol, end_time_ms, limit))
        return [vela(end_time_ms - m * MINUTO, close=100.0, vol=100.0) for m in range(limit)]


async def test_refill_gap_pagina_hacia_atras_hasta_cubrir_todo_el_hueco(orq):
    # Hueco de 500 minutos: con páginas de 200 velas hacen falta 3 llamadas
    # (200 + 200 + 100) para cubrirlo entero, no una sola.
    orq.rest = RestFalsoParaHuecos()
    await orq.ensure_profile("AAAUSDT", now_ms=14 * DIA)
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(14 * DIA)])
    )

    ahora = 14 * DIA + 500 * MINUTO
    # reconexión real: el snapshot trae la vela EN CURSO (ahora), como haría
    # el WS de verdad; eso es lo que fija la vela previa como límite de
    # backfill y activa la medición correcta del hueco (C4a/C4b).
    await orq.handle_ws_event(
        WsEvent(kind="snapshot", symbol="AAAUSDT", candles=[vela(ahora)])
    )
    await orq.refill_gap("AAAUSDT", now_ms=ahora)

    assert len(orq.rest.llamadas) == 3
    fin_pedidos = [end for (_sym, end, _lim) in orq.rest.llamadas]
    assert fin_pedidos == [ahora, ahora - 200 * MINUTO, ahora - 400 * MINUTO]

    # el minuto justo tras la última vela original ya está cubierto: no queda
    # hueco a mitad de camino.
    ts_cubiertos = {c.ts for c in orq.buffers["AAAUSDT"].all_closed()}
    assert (14 * DIA + MINUTO) in ts_cubiertos
    assert "AAAUSDT" in orq.dirty


async def test_refill_gap_cubre_el_hueco_en_el_multiplo_exacto_de_200_min(orq):
    # Caso límite: `minutos` (el tramo [desde, now_ms) en minutos) cae justo
    # en 200, un múltiplo exacto de MAX_HISTORY_LIMIT. Con una sola página
    # (ceil(200/200)=1) la vela de `desde` queda fuera si `endTime` resultase
    # ser exclusivo; la página de más garantiza que quede cubierta pase lo
    # que pase con esa semántica.
    orq.rest = RestFalsoParaHuecos()
    await orq.ensure_profile("AAAUSDT", now_ms=14 * DIA)
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(14 * DIA)])
    )

    desde = 14 * DIA + MINUTO
    ahora = desde + 200 * MINUTO  # minutos == 200 exactos
    await orq.handle_ws_event(
        WsEvent(kind="snapshot", symbol="AAAUSDT", candles=[vela(ahora)])
    )
    await orq.refill_gap("AAAUSDT", now_ms=ahora)

    assert len(orq.rest.llamadas) == 2  # 1 página no bastaría para cubrir `desde`
    fin_pedidos = [end for (_sym, end, _lim) in orq.rest.llamadas]
    assert fin_pedidos == [ahora, ahora - 200 * MINUTO]

    ts_cubiertos = {c.ts for c in orq.buffers["AAAUSDT"].all_closed()}
    assert desde in ts_cubiertos  # la vela justo tras el hueco no se pierde


async def test_refill_gap_cubre_el_hueco_en_el_multiplo_exacto_de_400_min(orq):
    # Mismo caso límite que arriba pero en el segundo múltiplo (400 min), para
    # confirmar que la página de más se añade en cada frontera, no solo en la
    # primera.
    orq.rest = RestFalsoParaHuecos()
    await orq.ensure_profile("AAAUSDT", now_ms=14 * DIA)
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(14 * DIA)])
    )

    desde = 14 * DIA + MINUTO
    ahora = desde + 400 * MINUTO  # minutos == 400 exactos
    await orq.handle_ws_event(
        WsEvent(kind="snapshot", symbol="AAAUSDT", candles=[vela(ahora)])
    )
    await orq.refill_gap("AAAUSDT", now_ms=ahora)

    assert len(orq.rest.llamadas) == 3  # 2 páginas no bastarían para cubrir `desde`
    fin_pedidos = [end for (_sym, end, _lim) in orq.rest.llamadas]
    assert fin_pedidos == [ahora, ahora - 200 * MINUTO, ahora - 400 * MINUTO]
    assert fin_pedidos[-1] == desde  # la última página termina justo en `desde`

    ts_cubiertos = {c.ts for c in orq.buffers["AAAUSDT"].all_closed()}
    assert desde in ts_cubiertos  # la vela justo tras el hueco no se pierde


async def test_refill_gap_respeta_el_tope_de_paginas_y_avisa(orq, caplog):
    # Hueco deliberadamente mayor de lo que MAX_PAGINAS_DE_RELLENO cubre: debe
    # pedir como máximo el tope de páginas y avisar del tramo sin cubrir, en
    # vez de encadenar peticiones sin fin.
    orq.rest = RestFalsoParaHuecos()
    await orq.ensure_profile("AAAUSDT", now_ms=14 * DIA)
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(14 * DIA)])
    )

    ahora = 14 * DIA + (Orchestrator.MAX_PAGINAS_DE_RELLENO + 5) * 200 * MINUTO
    with caplog.at_level(logging.WARNING):
        await orq.refill_gap("AAAUSDT", now_ms=ahora)

    assert len(orq.rest.llamadas) == Orchestrator.MAX_PAGINAS_DE_RELLENO
    avisos = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("AAAUSDT" in aviso for aviso in avisos)


async def test_refill_gap_no_pide_nada_si_el_buffer_esta_al_dia(orq):
    orq.rest = RestFalsoParaHuecos()
    await orq.ensure_profile("AAAUSDT", now_ms=14 * DIA)
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(14 * DIA)])
    )

    await orq.refill_gap("AAAUSDT", now_ms=14 * DIA + 60_000)

    assert orq.rest.llamadas == []


async def test_un_fallo_de_rest_al_rellenar_no_propaga(orq):
    class RestRoto:
        async def get_history_candles(self, symbol, end_time_ms, limit=200):
            raise RuntimeError("Bitget no responde")

    orq.rest = RestRoto()
    await orq.ensure_profile("AAAUSDT", now_ms=14 * DIA)
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(14 * DIA)])
    )
    await orq.refill_gap("AAAUSDT", now_ms=14 * DIA + 300 * MINUTO)  # no lanza


async def test_refill_gap_se_dispara_con_un_snapshot_real_tras_parada_larga(orq):
    """Regresión (a)+(b) de C4: un snapshot real de reconexión -no un
    `update`- es lo único que activa `reconnected`, y por eso es la única
    forma honesta de probar refill_gap end-to-end. Antes del fix, las velas
    del snapshot entraban en el buffer ANTES de marcar el símbolo, así que
    cuando el bucle principal drenaba `reconnected` y llamaba a refill_gap,
    buffer.current() ya era la vela recién llegada del propio snapshot:
    hueco_min ~= 0 y no se pedía nada por REST. Y aunque se pidiera, las
    velas traídas quedarían por detrás de `_current` y `upsert` las
    descartaría en silencio."""
    orq.rest = RestFalsoParaHuecos()
    await orq.ensure_profile("AAAUSDT", now_ms=14 * DIA)
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(14 * DIA)])
    )

    ahora = 14 * DIA + 500 * MINUTO  # el WS estuvo caído ~8h20
    # reconexión real: el WS manda un snapshot con la vela en curso ACTUAL,
    # no la vieja de antes de la caída.
    await orq.handle_ws_event(
        WsEvent(kind="snapshot", symbol="AAAUSDT", candles=[vela(ahora)])
    )
    assert "AAAUSDT" in orq.reconnected

    await orq.refill_gap("AAAUSDT", now_ms=ahora)

    assert orq.rest.llamadas != []  # el hueco real (500 min) sí debe disparar peticiones
    ts_cubiertos = {c.ts for c in orq.buffers["AAAUSDT"].all_closed()}
    assert (14 * DIA + MINUTO) in ts_cubiertos  # el hueco quedó relleno, no descartado


async def test_ensure_profile_rellena_el_hueco_en_un_reinicio_en_caliente(orq):
    """Regresión (c) de C4: con el perfil ya en disco (reinicio en
    caliente), ensure_profile debe pedir igualmente el histórico que falta
    desde la última vela guardada. Antes del fix, bootstrap_symbol -único
    llamador de plan_history_requests- solo se invocaba cuando NO había
    perfil guardado, así que con perfil en disco el hueco de la parada no
    se rellenaba nunca, contradiciendo la spec y el README."""
    orq.rest = RestFalsoParaHuecos()
    base = 14 * DIA
    orq.candle_repo.save_many("AAAUSDT", [vela(base)])  # última vela antes de la parada

    velas_perfil = [vela(d * DIA + m * MINUTO, vol=100.0) for d in range(14) for m in range(1440)]
    perfil = build_profile("AAAUSDT", velas_perfil, orq.cfg.profile)
    orq.profile_repo.save(perfil)  # perfil ya en disco: simula el reinicio en caliente

    ahora = base + 300 * MINUTO  # hueco de 5h desde la última vela persistida
    await orq.ensure_profile("AAAUSDT", now_ms=ahora)

    assert orq.rest.llamadas != []  # se pidió histórico real para tapar el hueco
    assert orq.bootstrapper.pedidos == []  # no se reconstruyó el perfil: ya era válido
    ts_guardados = {c.ts for c in orq.candle_repo.load("AAAUSDT", base)}
    assert (base + MINUTO) in ts_guardados  # el hueco quedó cubierto en SQLite
    ts_en_buffer = {c.ts for c in orq.buffers["AAAUSDT"].all_closed()}
    assert (base + MINUTO) in ts_en_buffer  # y seed_buffer lo recogió en el buffer


async def test_snapshot_marca_el_simbolo_como_reconectado(orq):
    # es la señal que consume el bucle principal para disparar refill_gap tras
    # una reconexión; un "update" normal no debe activarla.
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(14 * DIA)])
    )
    assert orq.reconnected == set()
    await orq.handle_ws_event(
        WsEvent(kind="snapshot", symbol="AAAUSDT", candles=[vela(14 * DIA + MINUTO)])
    )
    assert orq.reconnected == {"AAAUSDT"}


class RestFalsoParaTickers:
    def __init__(self, tickers):
        self._tickers = tickers
        self.llamadas = 0

    async def get_tickers(self):
        self.llamadas += 1
        return self._tickers


async def test_poll_tickers_guarda_y_marca_sucios_los_simbolos_con_buffer(orq):
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(14 * DIA)])
    )
    orq.dirty.clear()  # aislar el efecto de poll_tickers del que deja handle_ws_event

    ticker_nuevo = Ticker("AAAUSDT", 105.0, 6.4, 5e6, 100.0, 0.0001, 0)
    ticker_sin_buffer = Ticker("ZZZUSDT", 1.0, 1.0, 1e6, 1.0, 0.0001, 0)
    orq.rest = RestFalsoParaTickers([ticker_nuevo, ticker_sin_buffer])

    await orq.poll_tickers(now_ms=14 * DIA + MINUTO)

    # queda disponible para que evaluate() lo use al puntuar
    assert orq.tickers["AAAUSDT"] is ticker_nuevo
    assert "AAAUSDT" in orq.dirty
    # símbolo fuera del universo activo (sin buffer): se ignora
    assert "ZZZUSDT" not in orq.tickers


async def test_poll_tickers_no_propaga_fallo_de_rest(orq):
    class RestRoto:
        async def get_tickers(self):
            raise RuntimeError("Bitget no responde")

    orq.rest = RestRoto()
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(14 * DIA)])
    )
    orq.dirty.clear()

    await orq.poll_tickers(now_ms=14 * DIA)  # no lanza

    assert orq.tickers == {}
    assert orq.dirty == set()


class BootstrapperFallaLaPrimeraVez:
    """Falla en la primera llamada para un símbolo y tiene éxito en la
    segunda, para reproducir la Finding 1: un bootstrap fallido no debe
    dejar al símbolo congelado en su placeholder para siempre."""

    def __init__(self, cfg):
        self._cfg = cfg
        self.intentos: list[str] = []

    async def bootstrap_symbol(self, symbol, now_ms):
        self.intentos.append(symbol)
        if self.intentos.count(symbol) == 1:
            raise RuntimeError("Bitget no responde")
        velas = [vela(d * DIA + m * MINUTO, vol=100.0)
                 for d in range(14) for m in range(1440)]
        return build_profile(symbol, velas, self._cfg)

    def expect(self, symbols):
        pass

    def progress(self):
        return (len(self.intentos), len(self.intentos))

    def mark_loaded(self, symbol):
        pass


async def test_un_bootstrap_fallido_se_reintenta_en_el_siguiente_apply_universe(orq):
    """Regresión Finding 1: si el bootstrap de fondo lanza una excepción, el
    símbolo debe seguir siendo candidato a reintento en el siguiente
    apply_universe (misma lista `ordered`, símbolo aún presente), en vez de
    quedarse congelado en su placeholder para siempre porque el guard de
    dedup no distingue placeholder de perfil real."""
    orq.ws = WsFalso()
    orq.bootstrapper = BootstrapperFallaLaPrimeraVez(orq.cfg.profile)
    update = UniverseUpdate(
        symbols=frozenset({"AAAUSDT"}), added=frozenset({"AAAUSDT"}),
        removed=frozenset(), ordered=["AAAUSDT"],
    )

    await orq.apply_universe(update, now_ms=14 * DIA)
    await asyncio.sleep(0)  # deja correr el bootstrap de fondo (falla)
    assert orq.bootstrapper.intentos == ["AAAUSDT"]
    assert orq.profiles["AAAUSDT"].confidence == "low"  # sigue en el placeholder

    # segundo refresco de universo: mismo símbolo, sigue en `ordered`, como
    # hace UniverseSelector.select en cada ciclo con el universo activo.
    await orq.apply_universe(update, now_ms=14 * DIA + MINUTO)
    await asyncio.sleep(0)  # deja correr el segundo intento (éxito)

    assert orq.bootstrapper.intentos == ["AAAUSDT", "AAAUSDT"]
    assert orq.profiles["AAAUSDT"].confidence == "high"


async def test_apply_universe_no_espera_a_que_terminen_los_bootstraps(orq):
    """Regresión C1: en frío, apply_universe no debe bloquear ~25 minutos
    esperando ensure_profile de cada símbolo antes de suscribir el WS."""
    orq.ws = WsFalso()
    orq.bootstrapper = BootstrapperLento(orq.cfg.profile)
    update = UniverseUpdate(
        symbols=frozenset({"AAAUSDT"}), added=frozenset({"AAAUSDT"}),
        removed=frozenset(), ordered=["AAAUSDT"],
    )

    # si apply_universe esperase al bootstrap (que nunca se libera aquí),
    # esto colgaría hasta el timeout: fallo limpio en vez de bloquear la
    # suite entera.
    await asyncio.wait_for(orq.apply_universe(update, now_ms=14 * DIA), timeout=0.5)

    assert orq.ws.subscribed == [["AAAUSDT"]]  # el WS ya está suscrito...
    assert orq.bootstrapper.terminados == []  # ...aunque el bootstrap real no ha terminado

    orq.bootstrapper.evento.set()
    await asyncio.sleep(0)  # deja correr la tarea de fondo ya lanzada
    assert orq.bootstrapper.terminados == ["AAAUSDT"]


class BootstrapperExplota:
    """Bootstrapper cuyo bootstrap_symbol nunca debe invocarse: usado para
    demostrar que un arranque en caliente no pasa por el camino de fondo."""

    def __init__(self, cfg):
        self._cfg = cfg
        self.llamadas: list[str] = []
        self.cargados: list[str] = []

    async def bootstrap_symbol(self, symbol, now_ms):
        self.llamadas.append(symbol)
        raise AssertionError("no debería lanzarse un bootstrap real en caliente")

    def expect(self, symbols):
        pass

    def progress(self):
        return (len(self.cargados), len(self.cargados))

    def mark_loaded(self, symbol):
        self.cargados.append(symbol)


async def test_apply_universe_en_caliente_usa_el_perfil_de_disco_sin_placeholder(orq):
    """Regresión Finding 2: si el perfil ya existe en disco (arranque en
    caliente), apply_universe debe cargarlo síncronamente y usarlo
    directamente -sin placeholder ni tarea de fondo-, en vez de enrutar una
    lectura barata de SQLite por la maquinaria pensada para el bootstrap
    lento por REST."""
    orq.ws = WsFalso()
    orq.bootstrapper = BootstrapperExplota(orq.cfg.profile)

    velas = [vela(d * DIA + m * MINUTO, vol=100.0) for d in range(14) for m in range(1440)]
    perfil_real = build_profile("AAAUSDT", velas, orq.cfg.profile)
    orq.profile_repo.save(perfil_real)

    update = UniverseUpdate(
        symbols=frozenset({"AAAUSDT"}), added=frozenset({"AAAUSDT"}),
        removed=frozenset(), ordered=["AAAUSDT"],
    )
    await orq.apply_universe(update, now_ms=14 * DIA)

    # el perfil real quedó puesto de inmediato, sin pasar por el placeholder
    assert orq.profiles["AAAUSDT"].confidence == "high"
    assert orq.profiles["AAAUSDT"] == perfil_real
    # nunca se lanzó bootstrap real ni tarea de fondo para este símbolo
    assert orq.bootstrapper.llamadas == []
    assert orq.bootstrapper.cargados == ["AAAUSDT"]
    assert "AAAUSDT" not in orq._bootstrap_tasks


async def test_mark_loaded_se_refleja_en_progress(orq):
    """`Bootstrapper.mark_loaded` no tenía test directo: comprueba que un
    símbolo cargado de disco (sin pasar por bootstrap_symbol) cuenta como
    completado en `progress()`."""
    from scanner_volumen.app.bootstrap import Bootstrapper

    boot = Bootstrapper(
        rest=None, candle_repo=orq.candle_repo, profile_repo=orq.profile_repo,
        profile_cfg=orq.cfg.profile,
    )
    boot.expect(["AAAUSDT", "BBBUSDT"])
    assert boot.progress() == (0, 2)

    boot.mark_loaded("AAAUSDT")

    assert boot.progress() == (1, 2)
