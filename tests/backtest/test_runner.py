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


def insertar_senal(
    repo, symbol, ts, state, direction, vwap_distance=0.0,
    config_fingerprint="c" * 64, code_revision="test-rev",
):
    return repo.insert(
        metricas(symbol, ts, vwap_distance), desglose(direction), state,
        config_fingerprint=config_fingerprint, code_revision=code_revision,
    )


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
    assert data.incomplete_outcomes == 0


def test_load_backtest_data_cuenta_outcomes_con_ventana_incompleta(conn):
    """Hallazgo 4: `candles_seen < candles_expected` marca un resultado
    calculado sobre un hueco de datos. `load_backtest_data` no debe filtrar
    esas filas -esta herramienta mide, no depura- pero sí contarlas."""
    repo = SignalRepo(conn)
    sid = insertar_senal(repo, "AAAUSDT", 0, State.HOT, Direction.LONG)
    repo.save_outcome(sid, 5, price=10.2, return_pct=2.0, mfe_pct=2.0, mae_pct=0.0,
                       candles_seen=3, candles_expected=5)  # hueco de datos
    repo.save_outcome(sid, 15, price=10.4, return_pct=4.0, mfe_pct=4.0, mae_pct=0.0,
                       candles_seen=15, candles_expected=15)  # ventana completa

    data = load_backtest_data(repo)
    assert len(data.signals) == 1  # no se excluye nada
    assert data.incomplete_outcomes == 1


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
        repo, horizons=(5,), gap_minutes=30, legacy_cutoff_ts=10**15,
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
        repo, horizons=(5,), gap_minutes=30, legacy_cutoff_ts=10**15,
        min_episodes_for_significance=30, entry_rules=(regla_signal,),
    )
    combo = resultado.results[0]
    assert combo.per_signal.n == 1
    assert combo.per_signal.mean_pnl == pytest.approx(9.0)


def test_run_no_divide_episodio_por_senales_de_otra_direccion_filtradas(conn):
    """Hallazgo 1, de extremo a extremo: reproduce la forma real del caso
    (TUTUSDT ids 3-19, un tramo SHORT en medio de un tramo LONG). La racha
    física completa nunca tiene un hueco por encima de 30 min, pero bajo una
    regla */LONG las señales SHORT intermedias quedan filtradas y las LONG
    supervivientes quedan a 35 min entre sí -por encima del hueco
    configurado-. Antes del fix esto se contaba como DOS episodios; debe
    seguir siendo UNO, porque `total_episodes` (agrupado sobre TODAS las
    señales) y el episodio de la regla LONG deben coincidir en este caso."""
    repo = SignalRepo(conn)
    id_primera = insertar_senal(repo, "TUTUSDT", 0, State.HOT, Direction.LONG)
    insertar_senal(repo, "TUTUSDT", 10 * MINUTO, State.SIGNAL, Direction.SHORT)
    insertar_senal(repo, "TUTUSDT", 20 * MINUTO, State.SIGNAL, Direction.SHORT)
    id_ultima = insertar_senal(repo, "TUTUSDT", 35 * MINUTO, State.HOT, Direction.LONG)
    for sid in (id_primera, id_ultima):
        insertar_outcome(repo, sid, 5, return_pct=2.0)

    regla_long = EntryRule(label="HOT+ / LONG", min_state=State.HOT, direction="LONG")
    resultado = run(
        repo, horizons=(5,), gap_minutes=30, legacy_cutoff_ts=10**15,
        min_episodes_for_significance=30, entry_rules=(regla_long,),
    )

    assert resultado.total_episodes == 1  # la racha física completa
    combo = resultado.results[0]
    assert combo.per_episode.n == 1  # NO 2: mismo episodio bajo la regla LONG


# --- fade ---------------------------------------------------------------

def test_run_con_regla_fade_invierte_el_pnl_de_su_gemela_no_fade(conn):
    repo = SignalRepo(conn)
    sid = insertar_senal(repo, "AAAUSDT", 0, State.HOT, Direction.LONG)
    insertar_outcome(repo, sid, 5, return_pct=2.0, mfe_pct=2.5, mae_pct=-0.5)

    regla_normal = EntryRule(label="HOT+ / ALL", min_state=State.HOT, direction="ALL")
    regla_fade = EntryRule(
        label="FADE HOT+ / ALL", min_state=State.HOT, direction="ALL", fade=True
    )
    resultado = run(
        repo, horizons=(5,), gap_minutes=30, legacy_cutoff_ts=10**15,
        min_episodes_for_significance=30, entry_rules=(regla_normal, regla_fade),
    )
    normal, fade = resultado.results
    assert fade.per_episode.n == normal.per_episode.n == 1
    assert fade.per_episode.mean_pnl == pytest.approx(-normal.per_episode.mean_pnl)
    assert fade.per_episode.mean_favourable == pytest.approx(normal.per_episode.mean_adverse)
    assert fade.per_episode.mean_adverse == pytest.approx(normal.per_episode.mean_favourable)


def test_regla_fade_filtra_por_direccion_de_la_senal_original_no_por_la_operacion(conn):
    """El filtro de dirección de la regla se aplica a la señal tal cual fue
    grabada -qué señales se toman-, no a la operación resultante del fade
    -cómo se toman-. Una regla FADE/SHORT solo admite señales SHORT, aunque
    lo que se opere sea, en la práctica, un LONG."""
    repo = SignalRepo(conn)
    sid_long = insertar_senal(repo, "AAAUSDT", 0, State.HOT, Direction.LONG)
    sid_short = insertar_senal(repo, "BBBUSDT", 0, State.HOT, Direction.SHORT)
    insertar_outcome(repo, sid_long, 5, return_pct=1.0, mfe_pct=1.0, mae_pct=0.0)
    insertar_outcome(repo, sid_short, 5, return_pct=-1.0, mfe_pct=0.0, mae_pct=-1.0)

    regla_fade_short = EntryRule(
        label="FADE HOT+ / SHORT", min_state=State.HOT, direction="SHORT", fade=True
    )
    resultado = run(
        repo, horizons=(5,), gap_minutes=30, legacy_cutoff_ts=10**15,
        min_episodes_for_significance=30, entry_rules=(regla_fade_short,),
    )
    combo = resultado.results[0]
    assert combo.per_signal.n == 1  # solo la señal SHORT califica
    # SHORT con return_pct=-1.0 -> P&L normal +1.0 (gana); fade lo invierte a -1.0.
    assert combo.per_signal.mean_pnl == pytest.approx(-1.0)


def test_run_con_base_vacia_no_lanza(conn):
    repo = SignalRepo(conn)
    regla = EntryRule(label="HOT+ / ALL", min_state=State.HOT, direction="ALL")
    resultado = run(
        repo, horizons=(5,), gap_minutes=30, legacy_cutoff_ts=1000,
        min_episodes_for_significance=30, entry_rules=(regla,),
    )
    assert resultado.total_signals == 0
    assert resultado.total_episodes == 0
    assert resultado.results[0].per_episode.n == 0
