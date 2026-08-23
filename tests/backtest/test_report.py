from datetime import datetime, timezone

from scanner_volumen.backtest.entry_rules import EntryRule
from scanner_volumen.backtest.report import format_report
from scanner_volumen.backtest.runner import BacktestRun, ComboResult
from scanner_volumen.backtest.segmentation import (
    LEGACY_ANTES_DEL_CORTE, LEGACY_DESDE_EL_CORTE, ProvenanceGroup,
)
from scanner_volumen.backtest.stats import HorizonStats
from scanner_volumen.models import State

MINUTO = 60_000


def _stats(n, mean_pnl=1.0, mixed_direction_episodes=0):
    if n == 0:
        return HorizonStats(n=0, mean_pnl=None, median_pnl=None, win_rate=None,
                             mean_favourable=None, mean_adverse=None)
    return HorizonStats(n=n, mean_pnl=mean_pnl, median_pnl=mean_pnl, win_rate=0.5,
                         mean_favourable=2.0, mean_adverse=-1.0,
                         mixed_direction_episodes=mixed_direction_episodes)


def _run(results, segmentation=(), total_signals=0, total_episodes=0,
         min_episodes_for_significance=30, incomplete_outcomes=0):
    return BacktestRun(
        results=results,
        segmentation=segmentation,
        total_signals=total_signals, total_episodes=total_episodes,
        ts_min=0, ts_max=1000, gap_minutes=30,
        min_episodes_for_significance=min_episodes_for_significance,
        incomplete_outcomes=incomplete_outcomes,
    )


def test_marca_muestra_insuficiente_por_debajo_del_umbral():
    regla = EntryRule(label="HOT+ / ALL", min_state=State.HOT, direction="ALL")
    resultado = ComboResult(regla, 5, per_signal=_stats(10), per_episode=_stats(5))
    texto = format_report(_run([resultado], min_episodes_for_significance=30))
    assert "MUESTRA INSUFICIENTE" in texto


def test_no_marca_muestra_insuficiente_por_encima_del_umbral():
    regla = EntryRule(label="HOT+ / ALL", min_state=State.HOT, direction="ALL")
    resultado = ComboResult(regla, 5, per_signal=_stats(80), per_episode=_stats(30))
    texto = format_report(_run([resultado], min_episodes_for_significance=30))
    # esta fila concreta (30 episodios) no debe llevar la marca
    lineas = [l for l in texto.splitlines() if "HOT+ / ALL" in l and "5" in l]
    assert lineas
    assert not any("MUESTRA INSUFICIENTE" in l for l in lineas)


def test_nunca_emite_una_recomendacion():
    regla = EntryRule(label="HOT+ / ALL", min_state=State.HOT, direction="ALL")
    resultado = ComboResult(regla, 5, per_signal=_stats(80), per_episode=_stats(30))
    texto = format_report(_run([resultado]))
    minuscula = texto.lower()
    for palabra in ("recomiendo", "recomendado", "mejor regla", "recommend", "best rule"):
        assert palabra not in minuscula


def test_documenta_la_decision_sobre_stop_target():
    texto = format_report(_run([]))
    minuscula = texto.lower()
    assert "stop" in minuscula and "target" in minuscula
    assert "orden" in minuscula  # explica que no se conoce el orden de toque


def test_incluye_grupo_vacio_sin_lanzar_y_lo_marca_como_sin_datos():
    regla = EntryRule(label="EXTREME+ / SHORT", min_state=State.EXTREME, direction="SHORT")
    resultado = ComboResult(regla, 60, per_signal=_stats(0), per_episode=_stats(0))
    texto = format_report(_run([resultado]))
    assert "n/a" in texto


def test_incluye_la_seccion_de_segmentacion_con_los_conteos():
    grupos = (
        ProvenanceGroup(label=LEGACY_ANTES_DEL_CORTE, n_signals=5, n_episodes=2,
                         ts_min=1_787_004_306_142, ts_max=1_787_004_306_142),
        ProvenanceGroup(label=LEGACY_DESDE_EL_CORTE, n_signals=38, n_episodes=6,
                         ts_min=1_787_070_798_483, ts_max=1_787_070_798_999),
    )
    texto = format_report(_run([], segmentation=grupos))
    assert "5" in texto and "38" in texto
    assert "2026" in texto  # el ts de cada grupo se muestra formateado como fecha


def test_cabecera_resume_senales_y_episodios_totales():
    texto = format_report(_run([], total_signals=43, total_episodes=8))
    assert "43" in texto
    assert "8" in texto


# --- Hallazgo 3: episodios de dirección mixta -------------------------

def test_columna_mixtos_ep_aparece_con_el_conteo_de_la_combinacion():
    regla = EntryRule(label="HOT+ / ALL", min_state=State.HOT, direction="ALL")
    resultado = ComboResult(
        regla, 5, per_signal=_stats(10), per_episode=_stats(30, mixed_direction_episodes=3)
    )
    texto = format_report(_run([resultado]))
    assert "MIXTOS_EP" in texto
    lineas = [l for l in texto.splitlines() if "HOT+ / ALL" in l]
    assert lineas
    # columna MIXTOS_EP: ancho 9, alineada a la derecha -> "3" con 8 espacios delante
    assert any(str(3).rjust(9) in l for l in lineas)


def test_aviso_explica_la_convencion_de_promediar_direcciones_mixtas():
    texto = format_report(_run([]))
    minuscula = texto.lower()
    assert "long" in minuscula and "short" in minuscula
    assert "mixtos_ep" in minuscula or "mixto" in minuscula


def test_aviso_aclara_que_win_pct_ep_es_sobre_episodios_no_operaciones():
    texto = format_report(_run([]))
    minuscula = texto.lower()
    assert "win%_ep" in minuscula
    assert "episodio" in minuscula


# --- Hallazgo 4: filas de resultado incompleto -------------------------

def test_cabecera_muestra_el_conteo_de_resultados_incompletos():
    texto = format_report(_run([], incomplete_outcomes=7))
    assert "7" in texto
    assert "candles_seen" in texto and "candles_expected" in texto


def test_cabecera_muestra_cero_resultados_incompletos_de_forma_explicita():
    """Aunque hoy sean cero, la cifra debe imprimirse siempre -no solo
    cuando hay algo que reportar-, para que no pase desapercibido el día que
    deje de serlo."""
    texto = format_report(_run([], incomplete_outcomes=0))
    assert "Resultados incompletos" in texto


# --- Minor: la segmentación puede sumar por encima del total -----------

def test_segmentacion_documenta_que_puede_sumar_por_encima_del_total():
    grupos = (
        ProvenanceGroup(label=LEGACY_ANTES_DEL_CORTE, n_signals=5, n_episodes=8,
                         ts_min=0, ts_max=999),
        ProvenanceGroup(label=LEGACY_DESDE_EL_CORTE, n_signals=38, n_episodes=8,
                         ts_min=1000, ts_max=2000),
    )
    texto = format_report(_run([], segmentation=grupos, total_episodes=15))
    minuscula = texto.lower()
    assert "cruza el" in minuscula or "cruzar" in minuscula or "cada lado" in minuscula
