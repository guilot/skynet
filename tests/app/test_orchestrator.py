# tests/app/test_orchestrator.py
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
    await orq.refill_gap("AAAUSDT", now_ms=ahora)

    assert len(orq.rest.llamadas) == 3
    fin_pedidos = [end for (_sym, end, _lim) in orq.rest.llamadas]
    assert fin_pedidos == [ahora, ahora - 200 * MINUTO, ahora - 400 * MINUTO]

    # el minuto justo tras la última vela original ya está cubierto: no queda
    # hueco a mitad de camino.
    ts_cubiertos = {c.ts for c in orq.buffers["AAAUSDT"].all_closed()}
    assert (14 * DIA + MINUTO) in ts_cubiertos
    assert "AAAUSDT" in orq.dirty


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
