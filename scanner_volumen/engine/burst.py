# scanner_volumen/engine/burst.py
"""Aceleración de demanda y de precio.

Que el RVOL sea alto no basta: interesa si está creciendo. El demand burst
compara el RVOL de ahora con el de hace unos minutos; el z-score sitúa el
retorno actual frente a la volatilidad reciente del propio símbolo.
"""
from __future__ import annotations

import statistics

MUESTRAS_MINIMAS_Z = 8


def demand_burst(
    rvol_now: float | None,
    rvol_before: float | None,
    min_denominator: float = 0.5,
) -> float | None:
    """El denominador se acota por abajo: con un RVOL previo de 0.01 el cociente
    sería enorme sin que eso signifique aceleración real."""
    if rvol_now is None or rvol_before is None:
        return None
    denominador = max(rvol_before, min_denominator)
    if denominador <= 0:
        return None
    return rvol_now / denominador


def z_return(recent_returns: list[float], current: float) -> float | None:
    """Desviaciones típicas del retorno actual respecto a los recientes.

    Con menos de MUESTRAS_MINIMAS_Z valores el resultado no es interpretable, y
    con desviación cero la división es imposible: en ambos casos None.
    """
    if len(recent_returns) < MUESTRAS_MINIMAS_Z:
        return None
    desviacion = statistics.pstdev(recent_returns)
    if desviacion <= 0:
        return None
    return (current - statistics.fmean(recent_returns)) / desviacion
