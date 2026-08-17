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

# Umbral interno (no de negocio, por eso no vive en config.toml) de cuántos
# slots del día deben tener datos para que la mediana de sus medianas sea
# representativa del perfil entero: por debajo de esto la mediana quedaría
# sesgada hacia las horas que sí tuvieron muestra, igual que `_percentil`
# exige al menos un valor. Es la misma clase de garantía estadística que
# `confidence`, pero sobre cobertura de slots dentro del día en vez de días
# de histórico cubiertos.
MIN_SLOTS_POBLADOS_PARA_TIPICO = MINUTOS_POR_DIA // 2


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
        """Suma de las medianas desde el minuto 0 hasta `minute` inclusive.

        A diferencia de `baseline()`, que se niega (devuelve None) ante
        cualquier slot ausente o con mediana <= 0, aquí los slots ausentes se
        saltan y la suma continúa: solo se devuelve None si no se encontró
        ningún dato en todo el rango o si el total es <= 0. Se prefiere esta
        tolerancia porque `cumulative_baseline` es el denominador del RVOL de
        sesión, que cubre cientos de minutos; exigir que todos estén presentes
        haría que el RVOL de sesión desapareciera por la ausencia de un solo
        minuto. El coste de esta tolerancia es que un rango con huecos
        subestima ligeramente la referencia acumulada y, por tanto, sobrestima
        ligeramente el RVOL de sesión resultante.
        """
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

    def typical_volume(self) -> float | None:
        """Volumen típico por minuto de todo el perfil: la mediana de las
        medianas de los slots poblados.

        Es el número que decide si un libro tiene actividad suficiente para
        que su mediana sea una referencia de fiar (ver
        `UniverseConfig.min_profile_median_volume` y
        `Orchestrator._admite_libro`): a diferencia de `baseline()`, que
        resuelve un minuto concreto, este resume el perfil entero en un
        único escalar -el mismo número que ya usa el RVOL como denominador,
        solo que agregado para todo el día-. Medido en real: HUSDT reporta
        $10M de volumen 24h pero una mediana de minuto de $57; este método
        es el que expone ese $57, no el $10M.

        Devuelve None si menos de la mitad de los 1440 slots tienen datos
        (`MIN_SLOTS_POBLADOS_PARA_TIPICO`): con menos que eso la mediana
        quedaría sesgada hacia las horas que sí tuvieron muestra, en vez de
        representar el día completo. Es pura y no toca el reloj ni hace I/O:
        opera solo sobre `self.slots`, ya calculados por `build_profile`.
        """
        medianas = [slot.median for slot in self.slots if slot is not None]
        if len(medianas) < MIN_SLOTS_POBLADOS_PARA_TIPICO:
            return None
        return statistics.median(medianas)


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


def placeholder_profile(symbol: str) -> VolumeProfile:
    """Perfil provisional de baja confianza para un símbolo sin bootstrap
    todavía disponible (arranque en frío no bloqueante).

    Sin slots, así que `baseline`/`cumulative_baseline` siempre devuelven
    None; el símbolo se sigue puntuando desde el primer minuto vía el
    fallback de mediana rolling que `MetricsBuilder` ya aplica a cualquier
    perfil con `confidence != "high"`, en vez de quedarse sin puntuar hasta
    que termine la descarga real de histórico.
    """
    return VolumeProfile(
        symbol=symbol,
        slots=tuple(None for _ in range(MINUTOS_POR_DIA)),
        confidence="low",
        days_covered=0.0,
    )


def rolling_baseline(candles: list[Candle], n: int) -> float | None:
    """Referencia de emergencia para símbolos sin histórico suficiente:
    mediana de las últimas n velas cerradas."""
    if not candles or n <= 0:
        # candles[-n:] con n=0 devolvería la lista entera (candles[-0:] == candles[:]),
        # no un slice vacío: hay que cortar aquí para no calcular la mediana de
        # todo el histórico cuando se pretendía desactivar el fallback.
        return None
    ultimas = [c.quote_vol for c in candles[-n:]]
    if not ultimas:
        return None
    mediana = statistics.median(ultimas)
    return mediana if mediana > 0 else None
