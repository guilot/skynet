import pytest

from scanner_volumen.backtest.episodes import group_episodes
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
        qualifying_signals=[], all_episodes=[], outcomes_by_signal={}, horizon=5,
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
    episodios = group_episodes(señales, gap_minutes=30)
    stats_señal, _ = compute_combo_stats(señales, episodios, outcomes, horizon=5)
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
    episodios = group_episodes(señales, gap_minutes=30)
    stats_señal, stats_episodio = compute_combo_stats(
        señales, episodios, outcomes, horizon=5
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
    episodios = group_episodes(señales, gap_minutes=30)
    stats_señal, stats_episodio = compute_combo_stats(
        señales, episodios, outcomes, horizon=5
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
    episodios = group_episodes(señales, gap_minutes=30)
    stats_señal, _ = compute_combo_stats(señales, episodios, outcomes, horizon=5)
    assert stats_señal.win_rate == pytest.approx(0.5)


# --- Hallazgo 1: episodios agrupados sobre TODAS las señales, no solo las
# que califican para la regla -----------------------------------------

def test_episodio_no_se_divide_por_senales_intermedias_que_la_regla_excluye():
    """Reproduce la forma real del caso (TUTUSDT ids 3-19: un tramo SHORT en
    medio de un tramo LONG bajo una regla */LONG). La racha física completa
    del símbolo nunca tiene un hueco por encima de 30 min (10, 10 y 15 min
    entre señales consecutivas); pero si los episodios se agrupasen solo
    sobre las señales que califican (LONG), las señales SHORT intermedias
    desaparecerían y las dos LONG supervivientes quedarían a 35 min -por
    encima del hueco configurado- partiendo un único episodio en dos."""
    todas = [
        _senal(1, "TUTUSDT", 0),
        _senal(2, "TUTUSDT", 10 * 60_000, direction="SHORT"),
        _senal(3, "TUTUSDT", 20 * 60_000, direction="SHORT"),
        _senal(4, "TUTUSDT", 35 * 60_000),
    ]
    calificadas = [s for s in todas if s["direction"] == "LONG"]  # ids 1 y 4
    outcomes = {
        1: {5: _outcome(2.0, 2.0, 0.0)},
        4: {5: _outcome(4.0, 4.0, 0.0)},
    }
    episodios_totales = group_episodes(todas, gap_minutes=30)

    _, stats_episodio = compute_combo_stats(
        calificadas, episodios_totales, outcomes, horizon=5
    )

    assert stats_episodio.n == 1  # NO 2: es la misma racha física
    assert stats_episodio.mean_pnl == pytest.approx((2.0 + 4.0) / 2)


def test_agrupar_sobre_solo_las_calificadas_habria_partido_el_episodio():
    """Control negativo del test anterior: demuestra que el bug era real -
    agrupando episodios sobre el subconjunto filtrado (el comportamiento
    previo al fix), el mismo escenario sí se parte en dos episodios."""
    todas = [
        _senal(1, "TUTUSDT", 0),
        _senal(2, "TUTUSDT", 10 * 60_000, direction="SHORT"),
        _senal(3, "TUTUSDT", 20 * 60_000, direction="SHORT"),
        _senal(4, "TUTUSDT", 35 * 60_000),
    ]
    calificadas = [s for s in todas if s["direction"] == "LONG"]
    episodios_sobre_calificadas = group_episodes(calificadas, gap_minutes=30)
    assert len(episodios_sobre_calificadas) == 2  # el bug que el fix corrige


# --- Hallazgo 3: episodios que mezclan LONG y SHORT bajo una regla /ALL -

def test_compute_combo_stats_cuenta_episodios_de_direccion_mixta():
    """Bajo una regla /ALL, un LONG y un SHORT casi simultáneos del mismo
    símbolo caen en el mismo episodio y su P&L (ya ajustado por dirección)
    se promedia sin más -tienden a cancelarse por construcción-. El conteo
    de episodios mixtos debe reflejarlo."""
    señales = [
        _senal(1, "TUTUSDT", 0, direction="LONG"),
        _senal(2, "TUTUSDT", 5 * 60_000, direction="SHORT"),
        _senal(3, "CYSUSDT", 0, direction="LONG"),  # episodio de una sola dirección
    ]
    outcomes = {
        1: {5: _outcome(return_pct=3.0, mfe_pct=3.0, mae_pct=0.0)},
        2: {5: _outcome(return_pct=-2.5, mfe_pct=0.0, mae_pct=-2.5)},
        3: {5: _outcome(return_pct=1.0, mfe_pct=1.0, mae_pct=0.0)},
    }
    episodios = group_episodes(señales, gap_minutes=30)

    _, stats_episodio = compute_combo_stats(señales, episodios, outcomes, horizon=5)

    assert stats_episodio.n == 2  # TUTUSDT (mixto) + CYSUSDT (LONG puro)
    assert stats_episodio.mixed_direction_episodes == 1
    # TUTUSDT: pnl LONG=3.0, pnl SHORT ajustado=-(-2.5)=2.5 -> media 2.75
    assert stats_episodio.mean_pnl == pytest.approx((2.75 + 1.0) / 2)


# --- fade: negación/intercambio de las tres cantidades direccionales ---

def test_fade_invierte_el_pnl_direccional_de_un_long():
    # LONG que sube +2% -> P&L normal +2.0, fade -2.0.
    señales = [_senal(1, "AAA", 0, direction="LONG")]
    outcomes = {1: {5: _outcome(return_pct=2.0, mfe_pct=2.0, mae_pct=0.0)}}
    episodios = group_episodes(señales, gap_minutes=30)
    stats_señal, stats_episodio = compute_combo_stats(
        señales, episodios, outcomes, horizon=5, fade=True
    )
    assert stats_señal.mean_pnl == pytest.approx(-2.0)
    assert stats_episodio.mean_pnl == pytest.approx(-2.0)


def test_fade_invierte_el_pnl_direccional_de_un_short():
    # SHORT cuyo precio cae -3.0%: P&L normal de la posición es +3.0
    # (adjusted_return invierte el signo), y su fade es -3.0.
    señales = [_senal(2, "BBB", 0, direction="SHORT")]
    outcomes = {2: {5: _outcome(return_pct=-3.0, mfe_pct=0.0, mae_pct=-3.0)}}
    episodios = group_episodes(señales, gap_minutes=30)
    stats_señal_normal, _ = compute_combo_stats(
        señales, episodios, outcomes, horizon=5, fade=False
    )
    stats_señal_fade, _ = compute_combo_stats(
        señales, episodios, outcomes, horizon=5, fade=True
    )
    assert stats_señal_normal.mean_pnl == pytest.approx(3.0)
    assert stats_señal_fade.mean_pnl == pytest.approx(-3.0)


def test_fade_intercambia_favorable_y_adversa():
    señales = [_senal(1, "AAA", 0, direction="LONG")]
    outcomes = {1: {5: _outcome(return_pct=2.0, mfe_pct=5.0, mae_pct=-1.5)}}
    episodios = group_episodes(señales, gap_minutes=30)
    stats_señal_normal, _ = compute_combo_stats(
        señales, episodios, outcomes, horizon=5, fade=False
    )
    stats_señal_fade, _ = compute_combo_stats(
        señales, episodios, outcomes, horizon=5, fade=True
    )
    assert stats_señal_normal.mean_favourable == pytest.approx(5.0)
    assert stats_señal_normal.mean_adverse == pytest.approx(-1.5)
    # fade: la favorable pasa a ser la adversa original, y viceversa.
    assert stats_señal_fade.mean_favourable == pytest.approx(-1.5)
    assert stats_señal_fade.mean_adverse == pytest.approx(5.0)


def test_fade_agrupa_episodios_igual_que_sin_fade():
    """Una racha de un solo símbolo debe seguir colapsando a un único
    episodio bajo una regla fade -el agrupado de episodios no depende de
    cómo se calcule el P&L sobre cada señal."""
    señales = [
        _senal(1, "BBB", 0),
        _senal(2, "BBB", 60_000),
        _senal(3, "BBB", 2 * 60_000),
    ]
    outcomes = {
        1: {5: _outcome(-1.0, 0.5, -1.5)},
        2: {5: _outcome(-1.0, 0.5, -1.5)},
        3: {5: _outcome(-1.0, 0.5, -1.5)},
    }
    episodios = group_episodes(señales, gap_minutes=30)
    _, stats_episodio_normal = compute_combo_stats(
        señales, episodios, outcomes, horizon=5, fade=False
    )
    _, stats_episodio_fade = compute_combo_stats(
        señales, episodios, outcomes, horizon=5, fade=True
    )
    assert stats_episodio_normal.n == 1
    assert stats_episodio_fade.n == 1


def test_fade_y_no_fade_son_negativos_exactos_a_nivel_de_episodio():
    """La regla fade y su gemela no-fade sobre las mismas señales deben
    producir P&L de episodio que son exactamente negativos entre sí -es la
    propiedad de consistencia central del fade: no es un cálculo
    independiente, es la negación limpia del directional existente."""
    señales = [
        _senal(1, "AAA", 0, direction="LONG"),
        _senal(2, "BBB", 0, direction="SHORT"),
        _senal(3, "BBB", 60_000, direction="SHORT"),
    ]
    outcomes = {
        1: {5: _outcome(return_pct=2.0, mfe_pct=2.5, mae_pct=-0.5)},
        2: {5: _outcome(return_pct=-1.0, mfe_pct=1.0, mae_pct=-2.0)},
        3: {5: _outcome(return_pct=3.0, mfe_pct=3.5, mae_pct=-0.2)},
    }
    episodios = group_episodes(señales, gap_minutes=30)
    _, normal = compute_combo_stats(señales, episodios, outcomes, horizon=5, fade=False)
    _, fade = compute_combo_stats(señales, episodios, outcomes, horizon=5, fade=True)
    assert fade.n == normal.n
    assert fade.mean_pnl == pytest.approx(-normal.mean_pnl)
    assert fade.median_pnl == pytest.approx(-normal.median_pnl)


def test_regla_de_direccion_unica_nunca_produce_episodios_mixtos():
    """Por diseño, una regla /LONG o /SHORT solo deja pasar señales de esa
    dirección, así que ningún episodio calificado puede mezclar direcciones
    -mixed_direction_episodes debe quedarse en 0-."""
    señales = [
        _senal(1, "TUTUSDT", 0, direction="LONG"),
        _senal(2, "TUTUSDT", 5 * 60_000, direction="SHORT"),
    ]
    calificadas = [s for s in señales if s["direction"] == "LONG"]
    outcomes = {1: {5: _outcome(return_pct=3.0, mfe_pct=3.0, mae_pct=0.0)}}
    episodios = group_episodes(señales, gap_minutes=30)

    _, stats_episodio = compute_combo_stats(calificadas, episodios, outcomes, horizon=5)

    assert stats_episodio.n == 1
    assert stats_episodio.mixed_direction_episodes == 0
