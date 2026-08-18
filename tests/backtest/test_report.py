from datetime import datetime, timezone

from scanner_volumen.backtest.entry_rules import EntryRule
from scanner_volumen.backtest.report import format_report
from scanner_volumen.backtest.runner import BacktestRun, ComboResult
from scanner_volumen.backtest.segmentation import SegmentSplit
from scanner_volumen.backtest.stats import HorizonStats
from scanner_volumen.models import State

MINUTO = 60_000


def _stats(n, mean_pnl=1.0):
    if n == 0:
        return HorizonStats(n=0, mean_pnl=None, median_pnl=None, win_rate=None,
                             mean_favourable=None, mean_adverse=None)
    return HorizonStats(n=n, mean_pnl=mean_pnl, median_pnl=mean_pnl, win_rate=0.5,
                         mean_favourable=2.0, mean_adverse=-1.0)


def _run(results, segmentation=None, total_signals=0, total_episodes=0,
         min_episodes_for_significance=30):
    return BacktestRun(
        results=results,
        segmentation=segmentation or SegmentSplit(
            cutoff_ts=1000, n_before=0, n_after=0, episodes_before=0, episodes_after=0,
        ),
        total_signals=total_signals, total_episodes=total_episodes,
        ts_min=0, ts_max=1000, gap_minutes=30,
        min_episodes_for_significance=min_episodes_for_significance,
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
    split = SegmentSplit(cutoff_ts=1_787_004_306_142, n_before=5, n_after=38,
                          episodes_before=2, episodes_after=6)
    texto = format_report(_run([], segmentation=split))
    assert "5" in texto and "38" in texto
    assert "1787004306142" in texto or "2026" in texto  # el corte se muestra de algún modo


def test_cabecera_resume_senales_y_episodios_totales():
    texto = format_report(_run([], total_signals=43, total_episodes=8))
    assert "43" in texto
    assert "8" in texto
