"""Segmentación por cambio de scoring: las señales anteriores al corte se
puntuaron con una penalización de extensión sobre VWAP más débil y no son
estrictamente comparables con las posteriores. Este módulo solo mide y
expone el tamaño de cada lado -no intenta "corregir" ni combinar nada-, para
que se pueda inspeccionar."""
from __future__ import annotations

from dataclasses import dataclass

from scanner_volumen.backtest.episodes import group_episodes


@dataclass(frozen=True)
class SegmentSplit:
    cutoff_ts: int
    n_before: int
    n_after: int
    episodes_before: int
    episodes_after: int


def compute_segmentation(
    signals: list[dict], cutoff_ts: int, gap_minutes: float
) -> SegmentSplit:
    """`cutoff_ts` es exclusivo del lado "antes": una señal con `ts ==
    cutoff_ts` cuenta como "desde el corte", igual que el filtro de
    vencimiento en `SignalRepo.pending_outcomes` trata su límite como
    alcanzado. Los episodios de cada lado se agrupan por separado -un hueco
    que cruce el corte no debe unir un episodio de antes con uno de
    después-."""
    antes = [s for s in signals if s["ts"] < cutoff_ts]
    despues = [s for s in signals if s["ts"] >= cutoff_ts]
    return SegmentSplit(
        cutoff_ts=cutoff_ts,
        n_before=len(antes),
        n_after=len(despues),
        episodes_before=len(group_episodes(antes, gap_minutes)) if antes else 0,
        episodes_after=len(group_episodes(despues, gap_minutes)) if despues else 0,
    )
