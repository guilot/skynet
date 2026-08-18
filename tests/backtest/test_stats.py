import pytest

from scanner_volumen.backtest.stats import (
    HorizonStats, adjusted_favourable, adjusted_adverse, adjusted_return,
    compute_combo_stats,
)


# --- Requisito 1: ajuste por dirección --------------------------------

def test_retorno_ajustado_de_un_long_no_cambia_de_signo():
    assert adjusted_return("LONG", 3.5) == 3.5
    assert adjusted_return("LONG", -2.0) == -2.0


def test_retorno_ajustado_de_un_short_invierte_el_signo():
    # El caso central del requisito 1: un SHORT gana cuando el precio cae.
    # return_pct crudo de -3.5% (precio cayó) debe convertirse en +3.5% de
    # P&L para la posición.
    assert adjusted_return("SHORT", -3.5) == 3.5
    # y un movimiento en contra (precio sube) debe volverse una pérdida.
    assert adjusted_return("SHORT", 2.0) == -2.0


def test_favorable_y_adversa_de_un_long_usan_mfe_y_mae_sin_invertir():
    assert adjusted_favourable("LONG", mfe_pct=5.0, mae_pct=-1.5) == 5.0
    assert adjusted_adverse("LONG", mfe_pct=5.0, mae_pct=-1.5) == -1.5


def test_favorable_y_adversa_de_un_short_se_derivan_cruzadas_e_invertidas():
    # Requisito 1, textual: para un SHORT la excursión FAVORABLE se deriva
    # de mae_pct (el mínimo del precio, que para un short es lo bueno) y la
    # ADVERSA de mfe_pct (el máximo del precio, malo para un short).
    # mfe_pct=+5.0 (el precio subió 5%: malo para el short)
    # mae_pct=-8.0 (el precio bajó 8%: bueno para el short -> +8.0 favorable)
    assert adjusted_favourable("SHORT", mfe_pct=5.0, mae_pct=-8.0) == 8.0
    assert adjusted_adverse("SHORT", mfe_pct=5.0, mae_pct=-8.0) == -5.0


def test_neutral_se_trata_como_long_no_se_invierte():
    assert adjusted_return("NEUTRAL", -3.5) == -3.5


# --- Agregación segura (todo cociente comprueba el denominador) --------

def test_compute_combo_stats_con_grupo_vacio_no_lanza_y_devuelve_none():
    stats_señal, stats_episodio = compute_combo_stats(
        qualifying_signals=[], outcomes_by_signal={}, horizon=5, gap_minutes=30,
    )
    assert stats_señal.n == 0
    assert stats_señal.mean_pnl is None
    assert stats_señal.median_pnl is None
    assert stats_señal.win_rate is None
    assert stats_señal.mean_favourable is None
    assert stats_señal.mean_adverse is None
    assert stats_episodio.n == 0
    assert stats_episodio.mean_pnl is None


def _senal(id_, symbol, ts, direction="LONG"):
    return {"id": id_, "symbol": symbol, "ts": ts, "direction": direction}


def _outcome(return_pct, mfe_pct, mae_pct):
    return {"return_pct": return_pct, "mfe_pct": mfe_pct, "mae_pct": mae_pct}


def test_compute_combo_stats_ignora_senales_sin_outcome_para_ese_horizonte():
    señales = [_senal(1, "AAA", 0), _senal(2, "AAA", 60_000)]
    outcomes = {1: {5: _outcome(2.0, 3.0, -1.0)}}  # la señal 2 no tiene horizonte 5
    stats_señal, _ = compute_combo_stats(señales, outcomes, horizon=5, gap_minutes=30)
    assert stats_señal.n == 1
    assert stats_señal.mean_pnl == 2.0


def test_compute_combo_stats_por_senal_es_una_media_plana_dominada_por_repeticion():
    """Muestra por qué el requisito 2 hace falta: 1 señal aislada con +10% y
    3 señales del mismo episodio con -1% cada una. La media por señal (4
    observaciones) queda arrastrada hacia el -1% aunque en realidad hay solo
    dos episodios independientes."""
    señales = [
        _senal(1, "AAA", 0),
        _senal(2, "BBB", 0),
        _senal(3, "BBB", 60_000),
        _senal(4, "BBB", 2 * 60_000),
    ]
    outcomes = {
        1: {5: _outcome(10.0, 10.0, 0.0)},
        2: {5: _outcome(-1.0, 0.5, -1.5)},
        3: {5: _outcome(-1.0, 0.5, -1.5)},
        4: {5: _outcome(-1.0, 0.5, -1.5)},
    }
    stats_señal, stats_episodio = compute_combo_stats(
        señales, outcomes, horizon=5, gap_minutes=30
    )
    assert stats_señal.n == 4
    assert stats_señal.mean_pnl == pytest.approx((10.0 - 1.0 - 1.0 - 1.0) / 4)

    # por episodio: episodio AAA (+10%) y episodio BBB (media de sus 3
    # señales, -1%) pesan IGUAL -> media de episodios = (10 + (-1)) / 2
    assert stats_episodio.n == 2
    assert stats_episodio.mean_pnl == pytest.approx((10.0 - 1.0) / 2)


def test_compute_combo_stats_aplica_el_ajuste_de_direccion_antes_de_agregar():
    señales = [_senal(1, "AAA", 0, direction="SHORT")]
    outcomes = {1: {5: _outcome(return_pct=-4.0, mfe_pct=1.0, mae_pct=-4.0)}}
    stats_señal, stats_episodio = compute_combo_stats(
        señales, outcomes, horizon=5, gap_minutes=30
    )
    assert stats_señal.mean_pnl == 4.0  # -(-4.0)
    assert stats_señal.mean_favourable == 4.0  # -mae_pct
    assert stats_señal.mean_adverse == -1.0  # -mfe_pct
    assert stats_episodio.mean_pnl == 4.0


def test_win_rate_cuenta_estrictamente_positivos():
    señales = [_senal(1, "AAA", 0), _senal(2, "BBB", 0)]
    outcomes = {
        1: {5: _outcome(0.0, 0.0, 0.0)},   # cero no es una victoria
        2: {5: _outcome(1.0, 1.0, 0.0)},
    }
    stats_señal, _ = compute_combo_stats(señales, outcomes, horizon=5, gap_minutes=30)
    assert stats_señal.win_rate == pytest.approx(0.5)
