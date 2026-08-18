# tests/app/test_orchestrator.py
import asyncio
import logging
import time

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
    # I-2(b): `updated_ms` sigue al ts de la última vela conocida (aquí, la
    # del único evento de WS: `14 * DIA`), no al `now_ms` de evaluate(); el
    # segundo evaluate() no hizo nada de todos modos, así que cualquiera de
    # los dos valores demostraría "no hubo trabajo" -se deja el correcto-.
    assert orq.state.snapshot("AAAUSDT").updated_ms == 14 * DIA


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
    orq.profile_repo.save(perfil, now_ms=base)  # perfil ya en disco: simula el reinicio en caliente

    ahora = base + 300 * MINUTO  # hueco de 5h desde la última vela persistida
    await orq.ensure_profile("AAAUSDT", now_ms=ahora)

    assert orq.rest.llamadas != []  # se pidió histórico real para tapar el hueco
    assert orq.bootstrapper.pedidos == []  # no se reconstruyó el perfil: ya era válido
    ts_guardados = {c.ts for c in orq.candle_repo.load("AAAUSDT", base)}
    assert (base + MINUTO) in ts_guardados  # el hueco quedó cubierto en SQLite
    ts_en_buffer = {c.ts for c in orq.buffers["AAAUSDT"].all_closed()}
    assert (base + MINUTO) in ts_en_buffer  # y seed_buffer lo recogió en el buffer


async def test_apply_universe_rellena_el_hueco_en_un_reinicio_en_caliente(orq):
    """Regresión Fase 2 Tarea 3: el relleno de hueco de reinicio en caliente
    debe ser alcanzable desde `apply_universe`, el único punto de entrada
    real en producción (`__main__.py` solo llama a `apply_universe`;
    `ensure_profile` no se usa fuera de los tests). Antes del fix, el
    camino cálido de `apply_universe` hacía su propia copia de
    "load + mark_loaded + seed_buffer" sin pasar nunca por
    `_rellenar_hueco_de_reinicio`, así que ningún reinicio real rellenaba
    el hueco, pese a que `test_ensure_profile_rellena_el_hueco_en_un_reinicio_en_caliente`
    -que ejercita un camino que producción nunca toma- estaba en verde."""
    orq.ws = WsFalso()
    orq.rest = RestFalsoParaHuecos()
    base = 14 * DIA
    orq.candle_repo.save_many("AAAUSDT", [vela(base)])  # última vela antes de la parada

    velas_perfil = [vela(d * DIA + m * MINUTO, vol=3000.0) for d in range(14) for m in range(1440)]
    perfil = build_profile("AAAUSDT", velas_perfil, orq.cfg.profile)
    orq.profile_repo.save(perfil, now_ms=base)  # perfil ya en disco: simula el reinicio en caliente

    ahora = base + 300 * MINUTO  # hueco de 5h desde la última vela persistida
    update = UniverseUpdate(
        symbols=frozenset({"AAAUSDT"}), added=frozenset({"AAAUSDT"}),
        removed=frozenset(), ordered=["AAAUSDT"],
    )
    await orq.apply_universe(update, now_ms=ahora)
    # el relleno corre en segundo plano (no debe bloquear apply_universe,
    # ver el test de no-bloqueo más abajo): se deja correr la tarea.
    for _ in range(5):
        await asyncio.sleep(0)

    assert orq.rest.llamadas != []  # se pidió histórico real para tapar el hueco
    ts_guardados = {c.ts for c in orq.candle_repo.load("AAAUSDT", base)}
    assert (base + MINUTO) in ts_guardados  # el hueco quedó cubierto en SQLite
    ts_en_buffer = {c.ts for c in orq.buffers["AAAUSDT"].all_closed()}
    assert (base + MINUTO) in ts_en_buffer  # y llegó también al buffer


async def test_reconexion_tras_reinicio_en_caliente_mide_el_hueco_contra_lo_seedeado(orq):
    """Regresión Fase 2 Tarea 3: tras un reinicio en caliente, `seed_buffer`
    siembra el buffer vía `backfill`, que deliberadamente nunca asigna
    `_current` -ninguna de esas velas está "en curso" de verdad-, así que
    `buffer.current()` es `None` hasta que llega el primer evento de WS.
    Antes del fix, el primer snapshot de reconexión veía `current() is
    None`, lo confundía con un símbolo nuevo sin historia y borraba
    cualquier entrada de `_reconnect_gap_from`; `refill_gap` caía entonces
    a `buffer.current()`, que para ese momento ya era la propia vela que el
    snapshot acababa de traer -hueco ~= 0, sin pedir nada por REST-."""
    orq.rest = None  # apply_universe no debe disparar el relleno de fondo aquí:
    orq.ws = WsFalso()  # se quiere aislar el efecto de handle_ws_event + refill_gap
    base = 14 * DIA
    orq.candle_repo.save_many("AAAUSDT", [vela(base)])  # última vela antes de la parada

    velas_perfil = [vela(d * DIA + m * MINUTO, vol=3000.0) for d in range(14) for m in range(1440)]
    perfil = build_profile("AAAUSDT", velas_perfil, orq.cfg.profile)
    orq.profile_repo.save(perfil, now_ms=base)

    update = UniverseUpdate(
        symbols=frozenset({"AAAUSDT"}), added=frozenset({"AAAUSDT"}),
        removed=frozenset(), ordered=["AAAUSDT"],
    )
    await orq.apply_universe(update, now_ms=base)

    # tras el reinicio en caliente, antes de cualquier evento de WS: el
    # buffer tiene la vela histórica pero ninguna "en curso".
    assert orq.buffers["AAAUSDT"].current() is None
    assert {c.ts for c in orq.buffers["AAAUSDT"].all_closed()} == {base}

    orq.rest = RestFalsoParaHuecos()  # ahora sí, para medir refill_gap
    ahora = base + 300 * MINUTO  # el WS estuvo caído ~5h
    await orq.handle_ws_event(
        WsEvent(kind="snapshot", symbol="AAAUSDT", candles=[vela(ahora)])
    )
    # el hueco se capturó contra la última vela SEEDEADA, no contra None
    assert orq._reconnect_gap_from["AAAUSDT"] == base

    await orq.refill_gap("AAAUSDT", now_ms=ahora)

    assert orq.rest.llamadas != []  # el hueco real (300 min) sí disparó peticiones
    ts_cubiertos = {c.ts for c in orq.buffers["AAAUSDT"].all_closed()}
    assert (base + MINUTO) in ts_cubiertos  # el hueco quedó relleno, no descartado


class RestLentoParaHuecos:
    """REST cuyo `get_history_candles` no termina hasta que el test lo
    libera explícitamente, para comprobar que el relleno de hueco de
    reinicio en caliente no bloquea `apply_universe` (mismo espíritu que
    `BootstrapperLento` para el camino frío)."""

    def __init__(self):
        self.evento = asyncio.Event()
        self.llamadas: list[tuple] = []

    async def get_history_candles(self, symbol, end_time_ms, limit=200):
        self.llamadas.append((symbol, end_time_ms, limit))
        await self.evento.wait()
        return [vela(end_time_ms - m * MINUTO, close=100.0, vol=100.0) for m in range(limit)]


async def test_apply_universe_no_espera_al_relleno_de_hueco_en_caliente(orq):
    """Regresión Fase 2 Tarea 3: análoga a
    `test_apply_universe_no_espera_a_que_terminen_los_bootstraps`, pero para
    el camino cálido (perfil ya en disco). El relleno del hueco de reinicio
    corre por REST igual que el bootstrap frío, y tampoco debe bloquear
    `apply_universe`: el WS ya está suscrito y el símbolo ya puntúa con su
    perfil real desde el primer minuto, aunque el relleno tarde arbitrariamente."""
    orq.ws = WsFalso()
    orq.rest = RestLentoParaHuecos()
    base = 14 * DIA
    orq.candle_repo.save_many("AAAUSDT", [vela(base)])

    velas_perfil = [vela(d * DIA + m * MINUTO, vol=3000.0) for d in range(14) for m in range(1440)]
    perfil = build_profile("AAAUSDT", velas_perfil, orq.cfg.profile)
    orq.profile_repo.save(perfil, now_ms=base)

    ahora = base + 300 * MINUTO
    update = UniverseUpdate(
        symbols=frozenset({"AAAUSDT"}), added=frozenset({"AAAUSDT"}),
        removed=frozenset(), ordered=["AAAUSDT"],
    )

    # si apply_universe esperase al relleno (que nunca se libera aquí),
    # esto colgaría hasta el timeout: fallo limpio en vez de bloquear la
    # suite entera.
    await asyncio.wait_for(orq.apply_universe(update, now_ms=ahora), timeout=0.5)
    await asyncio.sleep(0)  # deja que la tarea de fondo arranque y llegue a su primer await

    assert orq.ws.subscribed == [["AAAUSDT"]]  # el WS ya está suscrito...
    assert orq.profiles["AAAUSDT"].confidence == "high"  # ...con el perfil real, no un placeholder
    assert orq.rest.llamadas != []  # ...el relleno ya se lanzó...
    # ...pero el buffer solo tiene lo que ya había en disco antes del hueco
    assert {c.ts for c in orq.buffers["AAAUSDT"].all_closed()} == {base}

    orq.rest.evento.set()
    for _ in range(5):
        await asyncio.sleep(0)  # deja correr el relleno de fondo ya lanzado

    ts_en_buffer = {c.ts for c in orq.buffers["AAAUSDT"].all_closed()}
    assert (base + MINUTO) in ts_en_buffer  # ahora sí, el hueco llegó al buffer


class CandleRepoFallaLaPrimeraVezAlGuardar:
    """Envoltorio sobre un CandleRepo real que falla en su primer save_many
    y tiene éxito después, para reproducir Finding 2: `_rellenar_hueco_en_fondo`
    tenía try/finally sin except, así que un fallo aquí escapaba de la tarea
    de fondo -el finally ya había popeado su única referencia, así que se
    recolectaba con una excepción sin recuperar en vez de solo loggear-."""

    def __init__(self, real):
        self._real = real
        self.intentos = 0

    def save_many(self, symbol, candles):
        self.intentos += 1
        if self.intentos == 1:
            raise RuntimeError("disco lleno")
        return self._real.save_many(symbol, candles)

    def __getattr__(self, nombre):
        return getattr(self._real, nombre)


async def test_relleno_de_hueco_en_fondo_fallido_no_escapa_y_se_reintenta(orq):
    """Regresión Finding 2: sin `except` en `_rellenar_hueco_en_fondo`, un
    fallo en `candle_repo.save_many` escapaba sin recuperar de la tarea de
    fondo, y encima el símbolo quedaba con un perfil real en `self.profiles`
    sin ninguna marca de placeholder -así que el guard de dedup de
    `apply_universe` lo daba por resuelto para siempre y el hueco quedaba sin
    cubrir el resto del proceso (el mismo fallo silencioso de métricas del
    Finding 1, por otra puerta)."""
    orq.ws = WsFalso()
    orq.rest = RestFalsoParaHuecos()
    base = 14 * DIA
    orq.candle_repo.save_many("AAAUSDT", [vela(base)])  # última vela antes de la parada

    velas_perfil = [vela(d * DIA + m * MINUTO, vol=3000.0) for d in range(14) for m in range(1440)]
    perfil = build_profile("AAAUSDT", velas_perfil, orq.cfg.profile)
    orq.profile_repo.save(perfil, now_ms=base)  # perfil ya en disco: simula el reinicio en caliente

    orq.candle_repo = CandleRepoFallaLaPrimeraVezAlGuardar(orq.candle_repo)

    ahora = base + 300 * MINUTO  # hueco de 5h desde la última vela persistida
    update = UniverseUpdate(
        symbols=frozenset({"AAAUSDT"}), added=frozenset({"AAAUSDT"}),
        removed=frozenset(), ordered=["AAAUSDT"],
    )
    await orq.apply_universe(update, now_ms=ahora)
    for _ in range(5):
        await asyncio.sleep(0)  # deja correr el relleno de fondo (falla)

    assert orq.candle_repo.intentos == 1  # el intento falló, no se retiró en silencio
    assert "AAAUSDT" not in orq._gap_fill_tasks  # el finally sí limpió la tarea
    assert orq.profiles["AAAUSDT"].confidence == "high"  # el perfil real se conserva
    # marcado para reintento: sin esto, el siguiente apply_universe lo daría
    # por resuelto (Finding 1 por otra puerta) y el hueco nunca se cubriría.
    assert "AAAUSDT" in orq._placeholder_symbols

    # segundo refresco de universo, mismo símbolo aún en `ordered`: reintenta
    # el relleno, que esta vez tiene éxito.
    await orq.apply_universe(update, now_ms=ahora + MINUTO)
    for _ in range(5):
        await asyncio.sleep(0)

    assert orq.candle_repo.intentos >= 2
    assert "AAAUSDT" not in orq._placeholder_symbols  # ya no necesita reintento
    ts_guardados = {c.ts for c in orq.candle_repo.load("AAAUSDT", base)}
    assert (base + MINUTO) in ts_guardados  # el hueco quedó cubierto en SQLite
    ts_en_buffer = {c.ts for c in orq.buffers["AAAUSDT"].all_closed()}
    assert (base + MINUTO) in ts_en_buffer  # y llegó también al buffer


async def test_un_segundo_snapshot_no_pisa_el_hueco_mas_grande_del_primero(orq):
    """Regresión ítem menor de Fase 2 Tarea 3: si llegan dos snapshots de
    reconexión antes de que el bucle principal drene `reconnected` y llame
    a `refill_gap` (p. ej. dos reconexiones seguidas de WS), el segundo no
    debe sobrescribir con `current().ts` -que para entonces ya es la vela
    que trajo el PRIMER snapshot- el baseline más antiguo y correcto que
    dejó el primero."""
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(14 * DIA)])
    )
    primer_snapshot = 14 * DIA + 300 * MINUTO
    await orq.handle_ws_event(
        WsEvent(kind="snapshot", symbol="AAAUSDT", candles=[vela(primer_snapshot)])
    )
    assert orq._reconnect_gap_from["AAAUSDT"] == 14 * DIA  # baseline real, antes de la caída

    segundo_snapshot = primer_snapshot + MINUTO
    await orq.handle_ws_event(
        WsEvent(kind="snapshot", symbol="AAAUSDT", candles=[vela(segundo_snapshot)])
    )
    # sin `setdefault`, esto pisaría el baseline con `primer_snapshot`
    # (la vela que ya es `current()` para cuando llega el segundo snapshot),
    # perdiendo la mayor parte del hueco real.
    assert orq._reconnect_gap_from["AAAUSDT"] == 14 * DIA


async def test_apply_universe_limpia_el_hueco_de_reconexion_y_el_historial_de_rvol_al_quitar_un_simbolo(orq):
    """Regresión ítems menores de Fase 2 Tarea 3: `apply_universe` ya
    limpiaba `buffers`, `profiles` y `_placeholder_symbols` al quitar un
    símbolo del universo, pero dejaba fugar `_reconnect_gap_from` (un
    símbolo que vuelve a entrar heredaría el baseline de una reconexión ya
    vieja), el historial de RVOL de `MetricsBuilder`, `self.tickers` y el
    estado de `StateMachine` (crecimiento sin límite con la rotación normal
    del universo, en los cuatro casos)."""
    await orq.ensure_profile("AAAUSDT", now_ms=14 * DIA)
    orq.set_ticker(Ticker("AAAUSDT", 100.0, 6.4, 5e6, 100.0, 0.0001, 0))
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(14 * DIA)])
    )
    await orq.handle_ws_event(
        WsEvent(kind="snapshot", symbol="AAAUSDT", candles=[vela(14 * DIA + MINUTO)])
    )
    orq.evaluate(now_ms=14 * DIA + 90_000)  # deja una muestra en _rvol_history y en _states
    assert "AAAUSDT" in orq._reconnect_gap_from
    assert "AAAUSDT" in orq._metrics._rvol_history
    assert "AAAUSDT" in orq.tickers
    assert "AAAUSDT" in orq._states._states

    update = UniverseUpdate(
        symbols=frozenset(), added=frozenset(), removed=frozenset({"AAAUSDT"}), ordered=[],
    )
    await orq.apply_universe(update, now_ms=14 * DIA + 100_000)

    assert "AAAUSDT" not in orq._reconnect_gap_from
    assert "AAAUSDT" not in orq._metrics._rvol_history
    assert "AAAUSDT" not in orq.tickers
    assert "AAAUSDT" not in orq._states._states


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
        # vol=3000.0, no 100.0: el filtro de libro fino (min_profile_median_volume,
        # config.toml) rechazaría un perfil con mediana 100 y este test no
        # tiene nada que ver con esa puerta -solo con el reintento tras un
        # bootstrap fallido.
        velas = [vela(d * DIA + m * MINUTO, vol=3000.0)
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

    velas = [vela(d * DIA + m * MINUTO, vol=3000.0) for d in range(14) for m in range(1440)]
    perfil_real = build_profile("AAAUSDT", velas, orq.cfg.profile)
    orq.profile_repo.save(perfil_real, now_ms=14 * DIA)

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


# --- I6: now_ms viene del reloj del exchange, nunca del de pared ---

async def test_now_ms_recurre_al_reloj_de_pared_hasta_el_primer_ticker(orq):
    # sin ningún ticker todavía, el único momento legítimo de usar el reloj
    # de pared es el arranque en frío.
    assert orq.now_ms(wall_clock_ms=123_456) == 123_456


async def test_now_ms_usa_el_ts_del_ticker_en_cuanto_llega_uno(orq):
    orq.set_ticker(Ticker("AAAUSDT", 100.0, 1.0, 5e6, 100.0, 0.0001, ts=500_000))
    # el reloj de pared "real" está deliberadamente muy lejos (deriva de
    # horas) y aun así now_ms debe devolver el ts del ticker, nunca el
    # argumento de respaldo: spec §13, "nunca la hora local".
    assert orq.now_ms(wall_clock_ms=999_999_999) == 500_000


async def test_now_ms_toma_el_maximo_entre_varios_tickers(orq):
    orq.set_ticker(Ticker("AAAUSDT", 100.0, 1.0, 5e6, 100.0, 0.0001, ts=500_000))
    orq.set_ticker(Ticker("BBBUSDT", 1.0, 1.0, 1e6, 1.0, 0.0001, ts=700_000))
    assert orq.now_ms(wall_clock_ms=0) == 700_000


async def test_now_ms_no_retrocede_si_el_reloj_del_exchange_salta_hacia_atras(orq):
    """El resto del motor (día en curso, historial de RVOL, cooldown de la
    máquina de estados, cómputo de huecos) asume `now_ms` monótono
    creciente. Ni una corrección de reloj en el exchange ni un ticker
    puntual desfasado de otro símbolo deben poder mover el tiempo hacia
    atrás."""
    orq.set_ticker(Ticker("AAAUSDT", 100.0, 1.0, 5e6, 100.0, 0.0001, ts=10_000))
    assert orq.now_ms(wall_clock_ms=0) == 10_000

    orq.set_ticker(Ticker("AAAUSDT", 100.0, 1.0, 5e6, 100.0, 0.0001, ts=5_000))
    assert orq.now_ms(wall_clock_ms=0) == 10_000  # no retrocede a 5_000


async def test_now_ms_sigue_avanzando_con_velas_del_ws_aunque_rest_falle(orq):
    """Regresión C-1: `now_ms` solo se alimentaba de `poll_tickers`. Si
    `/tickers` falla durante una caída larga mientras el WS sigue entregando
    velas, el reloj no debe congelarse -- eso ancla `outcomes.run_once` al
    minuto equivocado y deja crecer `rvol_session` sin límite contra un
    `cumulative_baseline` congelado (el mismo `inicio_dia`/`minuto_actual` de
    siempre)."""
    base = 14 * DIA
    orq.set_ticker(Ticker("AAAUSDT", 100.0, 1.0, 5e6, 100.0, 0.0001, ts=base))
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(base)])
    )
    assert orq.now_ms(wall_clock_ms=0) == base

    # /tickers está caído (ningún set_ticker más) pero el WS sigue vivo:
    # cada vela cerrada debe empujar el ratchet, no solo el último ticker bueno.
    for m in range(1, 11):
        await orq.handle_ws_event(
            WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(base + m * MINUTO)])
        )
        assert orq.now_ms(wall_clock_ms=0) >= base + m * MINUTO


async def test_ticker_con_ts_implausible_no_mueve_el_reloj(orq):
    """Regresión C-1: `parse_tickers` no valida `ts`, y `_clock_ms` es
    irrecuperable dentro de un proceso (el ratchet nunca retrocede) y ahora
    alimenta `candle_repo.prune(now_ms - 14d)`, un borrado masivo. Un ts de
    ticker disparatado (p. ej. un año en el futuro respecto al reloj local)
    no debe poder mover el ratchet."""
    base = 14 * DIA
    orq.set_ticker(Ticker("AAAUSDT", 100.0, 1.0, 5e6, 100.0, 0.0001, ts=base))
    assert orq.now_ms(wall_clock_ms=base) == base

    orq.set_ticker(
        Ticker("AAAUSDT", 100.0, 1.0, 5e6, 100.0, 0.0001, ts=base + 365 * DIA)
    )
    assert orq.now_ms(wall_clock_ms=base) == base  # se descarta, no se ratchetea

    # una vez descartado, un ticker de vuelta a un valor plausible sí avanza
    orq.set_ticker(
        Ticker("AAAUSDT", 100.0, 1.0, 5e6, 100.0, 0.0001, ts=base + MINUTO)
    )
    assert orq.now_ms(wall_clock_ms=base) == base + MINUTO


async def test_una_senal_persistida_usa_el_reloj_del_exchange_no_el_de_pared(orq):
    """I6, test de anclaje: `signals.ts` -el valor que `outcomes.run_once`
    compara después contra velas estampadas por el exchange (spec §13,
    "se usan siempre los timestamps del exchange, nunca la hora local")-
    debe salir de `Ticker.ts` vía `Orchestrator.now_ms`, nunca del reloj de
    pared que solo actúa de respaldo.

    Se reproduce aquí el mismo patrón que usa `__main__.bucle_evaluador`
    tras el fix (`orq.evaluate(orq.now_ms(ahora_ms()))`), con un reloj de
    pared simulado que se queda PARADO en `base` mientras el ticker sí
    avanza minuto a minuto, como en producción. Antes del fix, `__main__`
    pasaba `ahora_ms()` (el reloj de pared) directo a `evaluate`: con este
    mismo patrón, el ts persistido habría quedado clavado en
    `reloj_de_pared_parado` para siempre en vez de seguir al ticker."""
    base = 14 * DIA
    await orq.ensure_profile("AAAUSDT", now_ms=base)
    reloj_de_pared_parado = base

    for m in range(0, 120):
        orq.set_ticker(Ticker("AAAUSDT", 100.0, 14.0, 5e6, 100.0, 0.0001,
                               ts=base + m * MINUTO))
        await orq.handle_ws_event(
            WsEvent(kind="update", symbol="AAAUSDT",
                    candles=[vela(base + m * MINUTO, close=100.0, vol=100.0)])
        )
        orq.evaluate(now_ms=orq.now_ms(reloj_de_pared_parado))

    assert orq.signal_repo.recent(since_ms=0) == []  # calentamiento plano: nada persistido

    for i, m in enumerate(range(120, 128)):
        orq.set_ticker(Ticker("AAAUSDT", 130.0, 14.0, 5e6, 100.0, 0.0001,
                               ts=base + m * MINUTO))
        await orq.handle_ws_event(
            WsEvent(kind="update", symbol="AAAUSDT",
                    candles=[vela(base + m * MINUTO, close=100.0 + i * 3, vol=1200.0)])
        )
        orq.evaluate(now_ms=orq.now_ms(reloj_de_pared_parado))

    filas = orq.signal_repo.recent(since_ms=0)
    assert filas != []  # el pump sí escaló y persistió al menos una señal
    for f in filas:
        assert f["ts"] != reloj_de_pared_parado  # no vino del reloj de pared
        assert f["ts"] >= base + 120 * MINUTO      # siguió al ticker (reloj del exchange)


# --- I-2: el marcador de obsolescencia del dashboard debe seguir a la vela ---

async def test_updated_ms_sigue_al_ts_de_la_ultima_vela_no_al_de_evaluate(orq):
    """Regresión I-2(b): `poll_tickers` marca sucio cada símbolo con buffer
    cada `ticker_poll_seconds`, WS vivo o muerto, y antes del fix `evaluate`
    reescribía `updated_ms` con su propio `now_ms` (el de la evaluación) en
    cada pasada. Con el WS muerto y REST sano, ningún símbolo llegaba a
    quedar obsoleto nunca: `updated_ms` avanzaba igual que si las velas
    siguieran llegando. `updated_ms` debe seguir en cambio al ts de la
    última vela conocida del propio símbolo -deja de avanzar exactamente
    cuando el WS deja de entregar velas, que es la señal que este marcador
    existe para detectar-."""
    base = 14 * DIA
    await orq.ensure_profile("AAAUSDT", now_ms=base)
    orq.set_ticker(Ticker("AAAUSDT", 100.0, 1.0, 5e6, 100.0, 0.0001, 0))
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="AAAUSDT", candles=[vela(base)])
    )
    orq.evaluate(now_ms=base + 30_000)
    assert orq.state.snapshot("AAAUSDT").updated_ms == base

    # el WS deja de entregar velas, pero poll_tickers lo sigue marcando
    # sucio cada tick igualmente (como en producción, WS vivo o muerto).
    orq.dirty.add("AAAUSDT")
    orq.evaluate(now_ms=base + 5 * MINUTO)  # 5 min después, sin ninguna vela nueva

    assert orq.state.snapshot("AAAUSDT").updated_ms == base  # sigue anclado a la vela


# --- I2 + I4: mantenimiento diario (poda de velas + recálculo de perfil) ---

async def test_run_maintenance_poda_las_velas_fuera_de_la_ventana_retenida(orq):
    """I2: sin esto, `candles_1m` crece sin límite (spec §9 pide podado más
    allá de `history_days`); nada más en el sistema llama a `prune`."""
    # 20 velas, una por día (0..19): con history_days=14 (config.toml) y
    # ahora = día 19, el límite de retención cae en el día 5, así que las
    # de los días 0-4 deben desaparecer y las de los días 5-19 sobrevivir.
    orq.candle_repo.save_many("AAAUSDT", [vela(d * DIA) for d in range(20)])
    assert len(orq.candle_repo.load("AAAUSDT", since_ms=0)) == 20
    assert orq.cfg.profile.history_days == 14

    ahora = 19 * DIA
    await orq.run_maintenance(ahora)

    restantes = {c.ts for c in orq.candle_repo.load("AAAUSDT", since_ms=0)}
    assert restantes == {d * DIA for d in range(5, 20)}


async def test_run_maintenance_recalcula_el_perfil_de_volumen(orq):
    """I4: sin recálculo diario, el perfil de un proceso de larga vida queda
    anclado para siempre al que se descargó en el primer arranque (spec
    §4.3/§6.1: "profile_builder | arranque + diario"). Se compara hoy contra
    un día distinto con un patrón de volumen distinto para comprobar que el
    perfil realmente cambia, no solo que se vuelve a guardar el mismo."""
    base = 14 * DIA
    await orq.ensure_profile("AAAUSDT", now_ms=base)
    perfil_inicial = orq.profiles["AAAUSDT"]
    assert perfil_inicial.slots[0].median == pytest.approx(100.0)

    ahora = base + DIA
    # vol=3000.0, no 500.0: por debajo de min_profile_median_volume
    # (config.toml) el propio recálculo diario expulsaría a AAAUSDT del
    # universo activo (ver test_run_maintenance_excluye_un_simbolo_activo_
    # cuyo_perfil_se_ha_vuelto_fino) y `orq.profiles["AAAUSDT"]` no
    # existiría más abajo; este test solo quiere comprobar que el
    # recálculo diario sustituye el perfil, no ejercitar esa puerta.
    velas_nuevo_dia = [vela(base + m * MINUTO, vol=3000.0) for m in range(1440)]
    orq.candle_repo.save_many("AAAUSDT", velas_nuevo_dia)

    await orq.run_maintenance(ahora)

    assert orq.profiles["AAAUSDT"].slots[0].median == pytest.approx(3000.0)
    assert orq.profiles["AAAUSDT"] is not perfil_inicial
    fila = orq.profile_repo._conn.execute(
        "SELECT updated_ms FROM profile_meta WHERE symbol = ?", ("AAAUSDT",)
    ).fetchone()
    assert fila["updated_ms"] == ahora
    assert "AAAUSDT" in orq.dirty  # el dashboard debe reevaluar con el perfil nuevo


async def test_run_maintenance_no_recalcula_placeholders_en_bootstrap(orq):
    """Un símbolo con bootstrap real todavía en vuelo (placeholder) no debe
    recalcularse con lo poco que hubiera en SQLite: pisaría el resultado del
    bootstrap de fondo si terminara justo después."""
    orq.ws = WsFalso()
    orq.bootstrapper = BootstrapperLento(orq.cfg.profile)
    update = UniverseUpdate(
        symbols=frozenset({"AAAUSDT"}), added=frozenset({"AAAUSDT"}),
        removed=frozenset(), ordered=["AAAUSDT"],
    )
    await orq.apply_universe(update, now_ms=14 * DIA)
    assert "AAAUSDT" in orq._placeholder_symbols

    await orq.run_maintenance(14 * DIA + DIA)

    assert orq.profile_repo.load("AAAUSDT") is None  # no se guardó nada de fondo


async def test_run_maintenance_no_bloquea_el_event_loop(orq, monkeypatch):
    """Regresión I-3: sin ningún `await` en la parte real de trabajo,
    `run_maintenance` bloqueaba el hilo del event loop de principio a fin
    (medido: `candle_repo.load` de 14 días x 150 símbolos ≈ 9.1s, más
    `build_profile` x150 ≈ 2.0s) -- nada más podía correr durante esa
    ventana: ni lecturas de WS, ni polls de ticker, ni el dashboard, ni el
    propio latido del WS.

    Se reproduce con un `candle_repo.load` sintético pero de verdad
    bloqueante (`time.sleep`, no una corrutina lenta) para varios símbolos,
    y una tarea concurrente que solo cuenta cuántas veces consigue correr
    mientras tanto: con el bug, el event loop nunca vuelve a ella hasta que
    `run_maintenance` termina del todo, así que `vueltas` se queda en 0."""
    simbolos = [f"SYM{i}USDT" for i in range(6)]
    base = 14 * DIA
    for simbolo in simbolos:
        await orq.ensure_profile(simbolo, now_ms=base)
        orq.candle_repo.save_many(
            simbolo, [vela(base + m * MINUTO) for m in range(5)]
        )

    real_load = orq.candle_repo.load

    def load_lento(symbol, since_ms):
        time.sleep(0.05)  # I/O síncrono lento, bloqueante de verdad
        return real_load(symbol, since_ms)

    monkeypatch.setattr(orq.candle_repo, "load", load_lento)

    vueltas = 0

    async def latido():
        nonlocal vueltas
        while True:
            await asyncio.sleep(0.01)
            vueltas += 1

    tarea_latido = asyncio.create_task(latido())
    try:
        await orq.run_maintenance(base + DIA)
    finally:
        tarea_latido.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea_latido

    # 6 símbolos x 50ms bloqueantes = ~300ms de trabajo sintético: si el
    # event loop nunca se libera durante ese tramo, `latido` no consigue
    # correr ni una vez. Con el fix (asyncio.to_thread por símbolo), debe
    # intercalarse varias veces.
    assert vueltas > 0


# --- filtro de universo en dos etapas: prefiltro barato (volumen 24h) +
# puerta real (volumen típico del perfil, `VolumeProfile.typical_volume`) ---
#
# Medido contra el mercado real: HUSDT reporta $10M de volumen 24h pero una
# mediana de minuto de $57; VELVETUSDT reporta $15M con una mediana de $767.
# El volumen 24h no distingue "libro sostenible" de "dos ráfagas y
# silencio", así que la puerta real es `min_profile_median_volume`
# (config.toml), aplicada una vez existe el perfil -ver
# `Orchestrator._admite_libro`.

class BootstrapperLibroFino:
    """Bootstrapper cuyo perfil resultante depende de un volumen por
    minuto configurable por símbolo (`vol_por_simbolo`, mutable: el test de
    reingreso lo cambia entre llamadas para simular que un libro mejora),
    para ejercitar el filtro de libro fino sin tocar la red."""

    def __init__(self, cfg, vol_por_simbolo):
        self._cfg = cfg
        self._vol = vol_por_simbolo
        self.pedidos: list[str] = []

    async def bootstrap_symbol(self, symbol, now_ms):
        self.pedidos.append(symbol)
        vol = self._vol[symbol]
        velas = [vela(d * DIA + m * MINUTO, vol=vol)
                 for d in range(14) for m in range(1440)]
        return build_profile(symbol, velas, self._cfg)

    def expect(self, symbols):
        pass

    def progress(self):
        return (len(self.pedidos), len(self.pedidos))

    def mark_loaded(self, symbol):
        pass


async def test_un_simbolo_con_libro_fino_sale_del_universo_activo_y_uno_liquido_se_queda(orq):
    """Regresión del filtro de libro fino (Método, caso 1): un símbolo cuyo
    perfil.typical_volume() está por debajo de min_profile_median_volume
    debe salir del universo activo -desuscrito del WS, sin buffer, sin
    perfil, y sin aparecer en ranked() tras evaluar-; uno por encima del
    umbral debe seguir puntuando con normalidad."""
    orq.ws = WsFalso()
    # FINOUSDT: $50/min, muy por debajo del umbral de config.toml (2_000.0).
    # LIQUIDOUSDT: $5_000/min, claramente por encima.
    orq.bootstrapper = BootstrapperLibroFino(
        orq.cfg.profile, {"FINOUSDT": 50.0, "LIQUIDOUSDT": 5000.0}
    )
    update = UniverseUpdate(
        symbols=frozenset({"FINOUSDT", "LIQUIDOUSDT"}),
        added=frozenset({"FINOUSDT", "LIQUIDOUSDT"}),
        removed=frozenset(), ordered=["LIQUIDOUSDT", "FINOUSDT"],
    )
    await orq.apply_universe(update, now_ms=14 * DIA)
    for _ in range(5):
        await asyncio.sleep(0)  # deja correr los bootstraps de fondo

    # el símbolo de libro fino sale del universo activo...
    assert "FINOUSDT" not in orq.profiles
    assert "FINOUSDT" not in orq.buffers
    assert orq.ws.unsubscribed == [["FINOUSDT"]]
    assert "FINOUSDT" in orq._rejected_thin_book

    # ...mientras que el líquido se queda con su perfil real, suscrito
    assert orq.profiles["LIQUIDOUSDT"].confidence == "high"
    assert "LIQUIDOUSDT" in orq.buffers
    assert all("LIQUIDOUSDT" not in llamada for llamada in orq.ws.unsubscribed)

    # tras evaluar, solo el líquido aparece puntuado
    orq.set_ticker(Ticker("LIQUIDOUSDT", 100.0, 1.0, 5e6, 100.0, 0.0001, 0))
    await orq.handle_ws_event(
        WsEvent(kind="update", symbol="LIQUIDOUSDT",
                candles=[vela(14 * DIA, vol=5000.0)])
    )
    orq.evaluate(now_ms=14 * DIA + 30_000)
    simbolos_en_ranking = {s.symbol for s in orq.state.ranked()}
    assert simbolos_en_ranking == {"LIQUIDOUSDT"}


async def test_un_simbolo_rechazado_por_libro_fino_no_se_reintenta_en_cada_refresco_de_universo(orq):
    """Regresión del filtro de libro fino (Método, caso 2): el selector
    sigue trayendo al símbolo rechazado en `ordered` cada refresco (15 min,
    solo aplica el prefiltro barato de volumen 24h) -- sin memoria,
    apply_universe descargaría 14 días de histórico del mismo símbolo
    muerto en cada uno de esos refrescos."""
    orq.ws = WsFalso()
    orq.bootstrapper = BootstrapperLibroFino(orq.cfg.profile, {"FINOUSDT": 50.0})
    update = UniverseUpdate(
        symbols=frozenset({"FINOUSDT"}), added=frozenset({"FINOUSDT"}),
        removed=frozenset(), ordered=["FINOUSDT"],
    )
    await orq.apply_universe(update, now_ms=14 * DIA)
    for _ in range(5):
        await asyncio.sleep(0)
    assert orq.bootstrapper.pedidos == ["FINOUSDT"]
    assert "FINOUSDT" in orq._rejected_thin_book

    # segundo refresco de universo, 15 min después: el selector no sabe
    # nada del rechazo (solo aplicó su propio prefiltro), así que lo trae
    # otra vez en `ordered`.
    update2 = UniverseUpdate(
        symbols=frozenset({"FINOUSDT"}), added=frozenset(),
        removed=frozenset(), ordered=["FINOUSDT"],
    )
    await orq.apply_universe(update2, now_ms=14 * DIA + 15 * MINUTO)
    for _ in range(5):
        await asyncio.sleep(0)

    assert orq.bootstrapper.pedidos == ["FINOUSDT"]  # no se repitió el bootstrap


async def test_un_simbolo_rechazado_vuelve_si_el_mantenimiento_diario_ve_que_su_libro_mejoro(orq):
    """Regresión del filtro de libro fino (Método, caso 3): el rechazo no es
    permanente. El mantenimiento diario es el único punto que pide
    historial fresco por REST para un símbolo rechazado
    (`Orchestrator._reevaluar_rechazados`) -- si su volumen típico ya
    supera el umbral, vuelve al universo activo: perfil real, buffer
    sembrado, y de nuevo suscrito al WS."""
    orq.ws = WsFalso()
    vol_por_simbolo = {"FINOUSDT": 50.0}
    orq.bootstrapper = BootstrapperLibroFino(orq.cfg.profile, vol_por_simbolo)
    update = UniverseUpdate(
        symbols=frozenset({"FINOUSDT"}), added=frozenset({"FINOUSDT"}),
        removed=frozenset(), ordered=["FINOUSDT"],
    )
    await orq.apply_universe(update, now_ms=14 * DIA)
    for _ in range(5):
        await asyncio.sleep(0)
    assert "FINOUSDT" in orq._rejected_thin_book
    assert "FINOUSDT" not in orq.profiles

    # el libro mejora antes del siguiente mantenimiento diario
    vol_por_simbolo["FINOUSDT"] = 5000.0

    await orq.run_maintenance(14 * DIA + DIA)

    assert "FINOUSDT" not in orq._rejected_thin_book
    assert orq.profiles["FINOUSDT"].confidence == "high"
    assert "FINOUSDT" in orq.buffers
    assert orq.ws.subscribed[-1] == ["FINOUSDT"]  # re-suscrito
    assert orq.bootstrapper.pedidos.count("FINOUSDT") == 2  # rechazo inicial + reevaluación


async def test_run_maintenance_excluye_un_simbolo_activo_cuyo_perfil_se_ha_vuelto_fino(orq):
    """El filtro de libro fino es una propiedad del libro, no un evento de
    una sola vez en el bootstrap: el mismo punto de reevaluación diaria que
    revive a un símbolo rechazado también debe expulsar a uno activo cuyo
    perfil recalculado se ha secado."""
    orq.ws = WsFalso()
    orq.bootstrapper = BootstrapperLibroFino(orq.cfg.profile, {"AAAUSDT": 10.0})
    base = 14 * DIA
    velas_liquidas = [vela(d * DIA + m * MINUTO, vol=5000.0)
                       for d in range(14) for m in range(1440)]
    perfil_liquido = build_profile("AAAUSDT", velas_liquidas, orq.cfg.profile)
    orq.profile_repo.save(perfil_liquido, now_ms=base)

    update = UniverseUpdate(
        symbols=frozenset({"AAAUSDT"}), added=frozenset({"AAAUSDT"}),
        removed=frozenset(), ordered=["AAAUSDT"],
    )
    await orq.apply_universe(update, now_ms=base)
    assert orq.profiles["AAAUSDT"].confidence == "high"

    # el libro se seca: al día siguiente, las únicas velas nuevas en
    # candle_repo (lo único que el recálculo diario relee) son de volumen
    # muy bajo.
    ahora = base + DIA
    velas_finas = [vela(base + m * MINUTO, vol=10.0) for m in range(1440)]
    orq.candle_repo.save_many("AAAUSDT", velas_finas)

    await orq.run_maintenance(ahora)

    assert "AAAUSDT" not in orq.profiles
    assert "AAAUSDT" not in orq.buffers
    assert "AAAUSDT" in orq._rejected_thin_book
    assert orq.ws.unsubscribed == [["AAAUSDT"]]


# --- arranque en frío: no persistir signals antes de validar el libro ---
#
# El filtro de libro fino solo puede aplicarse una vez existe un perfil real
# (`_admite_libro`). En frío, el símbolo se puntúa desde el primer minuto con
# el placeholder + fallback de mediana rolling (C1), y puede llegar a HOT
# antes de que su bootstrap real termine. Medido en real: un arranque en frío
# de 52 min escribió 56 filas en `signals`, la mayoría de libros de $0-800/min
# que el filtro rechazó en cuanto llegó su perfil -exactamente el ruido que el
# filtro existe para excluir del dataset de calibración. `evaluate` no debe
# persistir nada para un símbolo hasta que `_admite_libro` lo haya evaluado
# contra `min_profile_median_volume`, sin importar el resultado.

class BootstrapperLentoConfigurable:
    """Como `BootstrapperLento` (no resuelve hasta que el test libera el
    evento), pero con volumen por minuto configurable, para controlar si el
    perfil resultante pasa o no el filtro de libro fino una vez resuelto."""

    def __init__(self, cfg, vol):
        self._cfg = cfg
        self._vol = vol
        self.terminados: list[str] = []
        self.evento = asyncio.Event()

    async def bootstrap_symbol(self, symbol, now_ms):
        await self.evento.wait()
        self.terminados.append(symbol)
        velas = [vela(d * DIA + m * MINUTO, vol=self._vol)
                 for d in range(14) for m in range(1440)]
        return build_profile(symbol, velas, self._cfg)

    def expect(self, symbols):
        pass

    def progress(self):
        return (len(self.terminados), len(self.terminados))

    def mark_loaded(self, symbol):
        pass


async def _calentar_y_pumpear(orq, symbol, base, vol_base, vol_pump):
    """Reproduce el mismo patrón que `test_un_pump_genera_transicion_y_se_
    persiste`: `rolling_fallback_candles` (120, config.toml) velas planas de
    calentamiento y luego 8 velas de pump con precio y volumen crecientes.
    Devuelve cuántas transiciones escalaron a HOT o más."""
    for m in range(0, 120):
        await orq.handle_ws_event(
            WsEvent(kind="update", symbol=symbol,
                    candles=[vela(base + m * MINUTO, close=100.0, vol=vol_base)])
        )
        orq.evaluate(now_ms=base + m * MINUTO + 59_000)

    escaladas_a_hot_o_mas = 0
    for i, m in enumerate(range(120, 128)):
        await orq.handle_ws_event(
            WsEvent(kind="update", symbol=symbol,
                    candles=[vela(base + m * MINUTO, close=100.0 + i * 0.8, vol=vol_pump)])
        )
        transiciones = orq.evaluate(now_ms=base + m * MINUTO + 59_000)
        escaladas_a_hot_o_mas += sum(
            1 for t in transiciones
            if t.escalated and t.current.rank >= State.HOT.rank
        )
    return escaladas_a_hot_o_mas


async def test_arranque_en_frio_no_persiste_antes_de_validar_libro_pero_si_despues(orq):
    """Método: un símbolo que llega a HOT (o más) mientras su perfil real
    todavía no se ha resuelto no debe persistir ninguna fila en `signals`; en
    cuanto se valida con un libro por encima del umbral, las escaladas
    posteriores sí deben persistirse con normalidad."""
    orq.ws = WsFalso()
    orq.bootstrapper = BootstrapperLentoConfigurable(orq.cfg.profile, vol=2500.0)  # libro líquido
    update = UniverseUpdate(
        symbols=frozenset({"AAAUSDT"}), added=frozenset({"AAAUSDT"}),
        removed=frozenset(), ordered=["AAAUSDT"],
    )
    await orq.apply_universe(update, now_ms=14 * DIA)  # bootstrap real no resuelto todavía
    assert orq.profiles["AAAUSDT"].confidence == "low"  # placeholder
    assert "AAAUSDT" in orq._pending_book_validation

    base = 14 * DIA
    orq.set_ticker(Ticker("AAAUSDT", 130.0, 14.0, 5e6, 100.0, 0.0001, 0))
    escaladas_sin_validar = await _calentar_y_pumpear(
        orq, "AAAUSDT", base, vol_base=100.0, vol_pump=1200.0
    )

    assert escaladas_sin_validar >= 1  # sí llegó a HOT o más...
    assert orq.state.snapshot("AAAUSDT").state.rank >= State.HOT.rank  # ...visible en el dashboard...
    assert orq.signal_repo.recent(since_ms=0) == []  # ...pero nada persistido: libro sin validar

    # el bootstrap real termina ahora, con un libro claramente líquido
    orq.bootstrapper.evento.set()
    for _ in range(5):
        await asyncio.sleep(0)

    assert orq.profiles["AAAUSDT"].confidence == "high"  # perfil real, ya no placeholder
    assert "AAAUSDT" not in orq._pending_book_validation  # validado

    # arranca en limpio para una nueva escalada inequívoca con el perfil real
    orq._states.forget("AAAUSDT")
    base2 = base + 200 * MINUTO
    escaladas_validado = await _calentar_y_pumpear(
        orq, "AAAUSDT", base2, vol_base=2500.0, vol_pump=30_000.0
    )

    assert escaladas_validado >= 1
    assert len(orq.signal_repo.recent(since_ms=0)) == escaladas_validado  # ya validado: sí persiste


async def test_un_simbolo_rechazado_por_libro_fino_nunca_persiste_aunque_hubiera_llegado_a_hot(orq):
    """Método: si cuando el perfil llega resulta tener libro fino, el símbolo
    debe quedar exactamente como si nunca hubiera llegado a HOT -ninguna fila
    en `signals`- sin importar que su score, mientras estaba sin validar,
    llegara a HOT o más."""
    orq.ws = WsFalso()
    orq.bootstrapper = BootstrapperLentoConfigurable(orq.cfg.profile, vol=50.0)  # libro fino
    update = UniverseUpdate(
        symbols=frozenset({"AAAUSDT"}), added=frozenset({"AAAUSDT"}),
        removed=frozenset(), ordered=["AAAUSDT"],
    )
    await orq.apply_universe(update, now_ms=14 * DIA)
    assert "AAAUSDT" in orq._pending_book_validation

    base = 14 * DIA
    orq.set_ticker(Ticker("AAAUSDT", 130.0, 14.0, 5e6, 100.0, 0.0001, 0))
    await _calentar_y_pumpear(orq, "AAAUSDT", base, vol_base=100.0, vol_pump=1200.0)

    assert orq.state.snapshot("AAAUSDT").state.rank >= State.HOT.rank  # llegó a HOT...
    assert orq.signal_repo.recent(since_ms=0) == []  # ...pero nada persistido: aún sin validar

    orq.bootstrapper.evento.set()  # el bootstrap resuelve ahora: libro fino
    for _ in range(5):
        await asyncio.sleep(0)

    assert "AAAUSDT" not in orq.profiles  # expulsado del universo activo
    assert "AAAUSDT" in orq._rejected_thin_book
    assert orq.signal_repo.recent(since_ms=0) == []  # nunca se persistió nada


class BootstrapperListingReciente:
    """Simula un listing reciente: solo 1 día de histórico (`confidence`
    queda en "low"), pero con volumen típico claramente por encima del umbral
    de libro fino -así el perfil resultante es real (no placeholder) y pasa
    por `_admite_libro`, aunque siga usando el fallback de mediana rolling
    para puntuar (mismo criterio que un placeholder: `confidence != "high"`)."""

    def __init__(self, cfg, vol):
        self._cfg = cfg
        self._vol = vol
        self.pedidos: list[str] = []

    async def bootstrap_symbol(self, symbol, now_ms):
        self.pedidos.append(symbol)
        velas = [vela(m * MINUTO, vol=self._vol) for m in range(1440)]
        return build_profile(symbol, velas, self._cfg)

    def expect(self, symbols):
        pass

    def progress(self):
        return (len(self.pedidos), len(self.pedidos))

    def mark_loaded(self, symbol):
        pass


async def test_un_simbolo_de_baja_confianza_ya_validado_si_persiste(orq):
    """Dos matices distintos: baja confianza (listing reciente) no es lo
    mismo que "sin validar". Un símbolo con poco histórico usa el mismo
    fallback de mediana rolling que un placeholder, pero si su perfil real ya
    pasó por `_admite_libro` (libro líquido), debe seguir persistiendo
    señales con normalidad -el diseño del proyecto es marcar los listings
    nuevos, nunca excluirlos."""
    orq.ws = WsFalso()
    orq.bootstrapper = BootstrapperListingReciente(orq.cfg.profile, vol=5000.0)
    update = UniverseUpdate(
        symbols=frozenset({"AAAUSDT"}), added=frozenset({"AAAUSDT"}),
        removed=frozenset(), ordered=["AAAUSDT"],
    )
    await orq.apply_universe(update, now_ms=14 * DIA)
    for _ in range(5):
        await asyncio.sleep(0)  # deja correr el bootstrap de fondo

    assert orq.profiles["AAAUSDT"].confidence == "low"  # listing reciente...
    assert "AAAUSDT" not in orq._placeholder_symbols  # ...pero perfil real, no placeholder
    assert "AAAUSDT" not in orq._pending_book_validation  # ...y ya validado contra el libro fino

    base = 14 * DIA
    orq.set_ticker(Ticker("AAAUSDT", 130.0, 14.0, 5e6, 100.0, 0.0001, 0))
    escaladas = await _calentar_y_pumpear(orq, "AAAUSDT", base, vol_base=100.0, vol_pump=1200.0)

    assert escaladas >= 1
    assert len(orq.signal_repo.recent(since_ms=0)) == escaladas  # baja confianza sí persiste
