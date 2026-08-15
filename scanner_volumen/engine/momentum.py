# scanner_volumen/engine/momentum.py
"""Retornos en varios horizontes y VWAP intradía."""
from __future__ import annotations

from scanner_volumen.engine.candles import CandleBuffer
from scanner_volumen.models import Candle


def pct_return(now: float, past: float | None) -> float | None:
    if past is None or past <= 0:
        return None
    return (now / past - 1) * 100


def returns(
    buffer: CandleBuffer, horizons: tuple[int, ...] = (1, 3, 5, 15, 30, 60)
) -> dict[int, float | None]:
    actual = buffer.current()
    if actual is None:
        return {h: None for h in horizons}
    return {h: pct_return(actual.close, buffer.close_at(h)) for h in horizons}


def vwap(candles: list[Candle]) -> float | None:
    """VWAP sobre precio típico (H+L+C)/3 ponderado por volumen en quote."""
    numerador = 0.0
    denominador = 0.0
    for c in candles:
        numerador += c.typical_price * c.quote_vol
        denominador += c.quote_vol
    if denominador <= 0:
        return None
    return numerador / denominador


def vwap_distance(price: float, vwap_value: float | None) -> float | None:
    if vwap_value is None or vwap_value <= 0:
        return None
    return (price / vwap_value - 1) * 100
