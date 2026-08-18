import pytest

from scanner_volumen.backtest.entry_rules import EntryRule
from scanner_volumen.backtest.runner import load_backtest_data, run
from scanner_volumen.engine.metrics import SymbolMetrics
from scanner_volumen.models import Direction, State
from scanner_volumen.scoring.score import ScoreBreakdown
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import SignalRepo

MINUTO = 60_000


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "test.db")
    yield c
    c.close()


def metricas(symbol, ts, vwap_distance=0.0):
    return SymbolMetrics(
        symbol=symbol, price=10.0, ret_1m=0.1, ret_3m=0.1, ret_5m=0.1,
        ret_15m=0.1, ret_30m=0.1, ret_1h=0.1, ret_24h=0.1,
        rvol_1m_closed=1.0, rvol_1m_live=1.0, rvol_5m=1.0, rvol_session=1.0,
        demand_burst=1.0, vwap=10.0, vwap_distance=vwap_distance, z_return=1.0,
        market_cap=1e8, volume_24h=1e7, open_interest=1.0, funding_rate=0.0,
        profile_confidence="high", ts=ts,
    )


def desglose(direction):
    return ScoreBreakdown(total=85.0, raw_total=85.0, momentum=35.0, demand=35.0,
                          structure=15.0, direction=direction, components={})


def insertar_senal(repo, symbol, ts, state, direction, vwap_distance=0.0):
    return repo.insert(metricas(symbol, ts, vwap_distance), desglose(direction), state)


def insertar_outcome(repo, signal_id, horizon, return_pct, mfe_pct=None, mae_pct=None):
    if mfe_pct is None:
        mfe_pct = max(return_pct, 0.0)
    if mae_pct is None:
        mae_pct = min(return_pct, 0.0)
    repo.save_outcome(signal_id, horizon, price=10.0, return_pct=return_pct,
                      mfe_pct=mfe_pct, mae_pct=mae_pct, candles_seen=horizon, candles_expected=horizon)


def test_load_backtest_data_indexa_outcomes_por_senal_y_horizonte(conn):
    repo = SignalRepo(conn)
    sid = insertar_senal(repo, "AAAUSDT", 0, State.HOT, Direction.LONG)
    insertar_outcome(repo, sid, 5, 2.0)
    insertar_outcome(repo, sid, 15, 4.0)

    data = load_backtest_data(repo)
    assert len(data.signals) == 1
    assert data.outcomes_by_signal[sid][5]["return_pct"] == 2.0
    assert data.outcomes_by_signal[sid][15]["return_pct"] == 4.0


def test_run_de_extremo_a_extremo_agrega_por_episodio_correctamente(conn):
    """Episodio A: TUTUSDT, 3 señales LONG a 10 min de separación, todas con
    +2% en el horizonte de 5 min -> media del episodio = 2.0.
    Episodio B: ZECUSDT, 1 señal SHORT con return_pct=-3.0 (el precio cayó)
    -> P&L ajustado = +3.0.
    Con la regla HOT+/ALL, horizonte 5: n_ep=2, media = (2.0 + 3.0) / 2 = 2.5,
    mientras que la media por-señal (4 observaciones, 3 de ellas del mismo
    episodio) queda arrastrada hacia +2.0 -- justo el sesgo que el requisito
    2 exige corregir."""
    repo = SignalRepo(conn)
    ids_a = [
        insertar_senal(repo, "TUTUSDT", i * 10 * MINUTO, State.HOT, Direction.LONG)
        for i in range(3)
    ]
    for sid in ids_a:
        insertar_outcome(repo, sid, 5, return_pct=2.0, mfe_pct=2.0, mae_pct=0.0)

    sid_b = insertar_senal(repo, "ZECUSDT", 0, State.SIGNAL, Direction.SHORT)
    insertar_outcome(repo, sid_b, 5, return_pct=-3.0, mfe_pct=0.0, mae_pct=-3.0)

    regla = EntryRule(label="HOT+ / ALL", min_state=State.HOT, direction="ALL")
    resultado = run(
        repo, horizons=(5,), gap_minutes=30, cutoff_ts=10**15,
        min_episodes_for_significance=30, entry_rules=(regla,),
    )

    assert resultado.total_signals == 4
    assert resultado.total_episodes == 2

    combo = resultado.results[0]
    assert combo.per_episode.n == 2
    assert combo.per_episode.mean_pnl == pytest.approx(2.5)
    assert combo.per_signal.n == 4
    assert combo.per_signal.mean_pnl == pytest.approx((2.0 * 3 + 3.0) / 4)


def test_run_filtra_por_regla_de_entrada(conn):
    repo = SignalRepo(conn)
    sid_hot = insertar_senal(repo, "AAAUSDT", 0, State.HOT, Direction.LONG)
    sid_signal = insertar_senal(repo, "BBBUSDT", 0, State.SIGNAL, Direction.LONG)
    insertar_outcome(repo, sid_hot, 5, return_pct=1.0)
    insertar_outcome(repo, sid_signal, 5, return_pct=9.0)

    regla_signal = EntryRule(label="SIGNAL+ / ALL", min_state=State.SIGNAL, direction="ALL")
    resultado = run(
        repo, horizons=(5,), gap_minutes=30, cutoff_ts=10**15,
        min_episodes_for_significance=30, entry_rules=(regla_signal,),
    )
    combo = resultado.results[0]
    assert combo.per_signal.n == 1
    assert combo.per_signal.mean_pnl == pytest.approx(9.0)


def test_run_con_base_vacia_no_lanza(conn):
    repo = SignalRepo(conn)
    regla = EntryRule(label="HOT+ / ALL", min_state=State.HOT, direction="ALL")
    resultado = run(
        repo, horizons=(5,), gap_minutes=30, cutoff_ts=1000,
        min_episodes_for_significance=30, entry_rules=(regla,),
    )
    assert resultado.total_signals == 0
    assert resultado.total_episodes == 0
    assert resultado.results[0].per_episode.n == 0
