# scanner_volumen/engine/rvol.py
"""Volumen relativo.

La distinción central: una vela en curso no es comparable con el baseline de una
vela completa. `rvol_closed` mide sobre velas cerradas y es lo que dispara
señales; `rvol_live` proyecta la vela en curso y es lo que muestra que algo está
ocurriendo ahora, pero solo pasado un mínimo de segundos, porque en el segundo 2
un único trade produciría un valor absurdo.
"""
from __future__ import annotations

from scanner_volumen.engine.profile import VolumeProfile, minute_of_day
from scanner_volumen.models import Candle

SEGUNDOS_POR_MINUTO = 60.0


def rvol_closed(volume: float, baseline: float | None) -> float | None:
    if baseline is None or baseline <= 0:
        return None
    return volume / baseline


def rvol_live(
    partial_volume: float,
    baseline: float | None,
    elapsed_seconds: float,
    min_elapsed_seconds: float,
) -> float | None:
    if baseline is None or baseline <= 0:
        return None
    if elapsed_seconds < min_elapsed_seconds:
        return None
    fraccion = min(1.0, elapsed_seconds / SEGUNDOS_POR_MINUTO)
    if fraccion <= 0:
        return None
    return partial_volume / (baseline * fraccion)


def rvol_window(candles: list[Candle], profile: VolumeProfile) -> float | None:
    """RVOL agregado de una ventana de velas cerradas (típicamente 5).

    Suma volúmenes y baselines antes de dividir, en lugar de promediar RVOLs
    individuales: así una vela con baseline diminuto no domina el resultado.
    """
    if not candles:
        return None
    volumen_total = 0.0
    baseline_total = 0.0
    for c in candles:
        base = profile.baseline(minute_of_day(c.ts))
        if base is None:
            continue
        volumen_total += c.quote_vol
        baseline_total += base
    if baseline_total <= 0:
        return None
    return volumen_total / baseline_total


def rvol_session(
    cumulative_volume: float, cumulative_baseline: float | None
) -> float | None:
    if cumulative_baseline is None or cumulative_baseline <= 0:
        return None
    return cumulative_volume / cumulative_baseline
