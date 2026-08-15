# scanner_volumen/engine/profile.py
"""Perfil de volumen intradía: cuánto volumen es normal en cada minuto del día.

Se usa mediana y no media porque una sola vela extrema desplaza la media lo
bastante como para que el RVOL deje de señalar nada. El suavizado por ventana
existe porque 14 días dan solo 14 muestras por minuto, insuficientes para una
mediana estable.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from scanner_volumen.config import ProfileConfig
from scanner_volumen.models import Candle

MINUTO_MS = 60_000
MINUTOS_POR_DIA = 1440


def minute_of_day(ts_ms: int) -> int:
    return (ts_ms // MINUTO_MS) % MINUTOS_POR_DIA


@dataclass(frozen=True)
class SlotStats:
    median: float
    p75: float
    p90: float
    p95: float
    samples: int


@dataclass(frozen=True)
class VolumeProfile:
    symbol: str
    slots: tuple[SlotStats | None, ...]
    confidence: str  # "high" o "low"
    days_covered: float

    def baseline(self, minute: int) -> float | None:
        """Volumen normal de ese minuto. None si no hay dato o si es cero:
        dividir por cero haría explotar el RVOL."""
        slot = self.slots[minute % MINUTOS_POR_DIA]
        if slot is None or slot.median <= 0:
            return None
        return slot.median

    def cumulative_baseline(self, minute: int) -> float | None:
        """Suma de las medianas desde el minuto 0 hasta `minute` inclusive."""
        total = 0.0
        vistos = 0
        for m in range(min(minute, MINUTOS_POR_DIA - 1) + 1):
            slot = self.slots[m]
            if slot is not None:
                total += slot.median
                vistos += 1
        if vistos == 0 or total <= 0:
            return None
        return total


def _percentil(datos_ordenados: list[float], q: float) -> float:
    """Interpolación lineal entre los dos valores más cercanos."""
    if not datos_ordenados:
        raise ValueError("no se puede calcular un percentil de una lista vacía")
    if len(datos_ordenados) == 1:
        return datos_ordenados[0]
    pos = q * (len(datos_ordenados) - 1)
    bajo = int(pos)
    alto = min(bajo + 1, len(datos_ordenados) - 1)
    frac = pos - bajo
    return datos_ordenados[bajo] * (1 - frac) + datos_ordenados[alto] * frac


def build_profile(
    symbol: str, candles: list[Candle], cfg: ProfileConfig
) -> VolumeProfile:
    por_minuto: list[list[float]] = [[] for _ in range(MINUTOS_POR_DIA)]
    for c in candles:
        por_minuto[minute_of_day(c.ts)].append(c.quote_vol)

    ventana = cfg.smoothing_window_minutes
    slots: list[SlotStats | None] = []
    for m in range(MINUTOS_POR_DIA):
        muestras: list[float] = []
        for desplazamiento in range(-ventana, ventana + 1):
            # el módulo hace que la ventana dé la vuelta a medianoche
            muestras.extend(por_minuto[(m + desplazamiento) % MINUTOS_POR_DIA])
        if not muestras:
            slots.append(None)
            continue
        muestras.sort()
        slots.append(
            SlotStats(
                median=statistics.median(muestras),
                p75=_percentil(muestras, 0.75),
                p90=_percentil(muestras, 0.90),
                p95=_percentil(muestras, 0.95),
                samples=len(muestras),
            )
        )

    if candles:
        rango_ms = max(c.ts for c in candles) - min(c.ts for c in candles)
        dias = rango_ms / (MINUTOS_POR_DIA * MINUTO_MS)
    else:
        dias = 0.0

    confianza = "high" if dias >= cfg.min_days_for_confidence else "low"
    return VolumeProfile(
        symbol=symbol,
        slots=tuple(slots),
        confidence=confianza,
        days_covered=dias,
    )


def rolling_baseline(candles: list[Candle], n: int) -> float | None:
    """Referencia de emergencia para símbolos sin histórico suficiente:
    mediana de las últimas n velas cerradas."""
    if not candles:
        return None
    ultimas = [c.quote_vol for c in candles[-n:]]
    if not ultimas:
        return None
    mediana = statistics.median(ultimas)
    return mediana if mediana > 0 else None
