"""Orquesta la carga de datos y el cálculo de todas las combinaciones
(regla de entrada x horizonte de salida), sin tocar disco fuera de
`SignalRepo` ni formatear texto (eso vive en `report.py`)."""
from __future__ import annotations

from dataclasses import dataclass

from scanner_volumen.backtest.entry_rules import EntryRule, default_entry_rules
from scanner_volumen.backtest.episodes import group_episodes
from scanner_volumen.backtest.segmentation import SegmentSplit, compute_segmentation
from scanner_volumen.backtest.stats import HorizonStats, compute_combo_stats
from scanner_volumen.storage.repos import SignalRepo


@dataclass(frozen=True)
class BacktestData:
    """Datos crudos ya cargados: señales + resultados indexados por
    signal_id -> horizonte. Separado de `run` para poder probar la
    agregación sin tocar disco."""

    signals: list[dict]
    outcomes_by_signal: dict[int, dict[int, dict]]


def load_backtest_data(signal_repo: SignalRepo) -> BacktestData:
    señales = signal_repo.all_signals()
    outcomes_by_signal: dict[int, dict[int, dict]] = {}
    for fila in signal_repo.all_outcomes():
        outcomes_by_signal.setdefault(fila["signal_id"], {})[fila["horizon_min"]] = fila
    return BacktestData(signals=señales, outcomes_by_signal=outcomes_by_signal)


@dataclass(frozen=True)
class ComboResult:
    rule: EntryRule
    horizon: int
    per_signal: HorizonStats
    per_episode: HorizonStats


def compute_all(
    data: BacktestData,
    horizons: tuple[int, ...],
    entry_rules: tuple[EntryRule, ...],
    gap_minutes: float,
) -> list[ComboResult]:
    resultados: list[ComboResult] = []
    for regla in entry_rules:
        calificados = [
            s for s in data.signals
            if regla.matches(
                state=s["state"], direction=s["direction"],
                vwap_distance=s["vwap_distance"],
            )
        ]
        for horizonte in horizons:
            stats_señal, stats_episodio = compute_combo_stats(
                calificados, data.outcomes_by_signal, horizonte, gap_minutes
            )
            resultados.append(ComboResult(regla, horizonte, stats_señal, stats_episodio))
    return resultados


@dataclass(frozen=True)
class BacktestRun:
    """Resultado completo listo para formatear (ver `report.py`)."""

    results: list[ComboResult]
    segmentation: SegmentSplit
    total_signals: int
    total_episodes: int
    ts_min: int | None
    ts_max: int | None
    gap_minutes: float
    min_episodes_for_significance: int


def run(
    signal_repo: SignalRepo,
    horizons: tuple[int, ...],
    gap_minutes: float,
    cutoff_ts: int,
    min_episodes_for_significance: int,
    entry_rules: tuple[EntryRule, ...] | None = None,
) -> BacktestRun:
    data = load_backtest_data(signal_repo)
    reglas = entry_rules if entry_rules is not None else default_entry_rules()
    resultados = compute_all(data, horizons, reglas, gap_minutes)
    segmentacion = compute_segmentation(data.signals, cutoff_ts, gap_minutes)
    episodios_totales = group_episodes(data.signals, gap_minutes) if data.signals else []
    return BacktestRun(
        results=resultados,
        segmentation=segmentacion,
        total_signals=len(data.signals),
        total_episodes=len(episodios_totales),
        ts_min=min((s["ts"] for s in data.signals), default=None),
        ts_max=max((s["ts"] for s in data.signals), default=None),
        gap_minutes=gap_minutes,
        min_episodes_for_significance=min_episodes_for_significance,
    )
