"""Estructuras de datos compartidas por todo el scanner."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class State(str, Enum):
    NORMAL = "NORMAL"
    WATCH = "WATCH"
    HOT = "HOT"
    SIGNAL = "SIGNAL"
    EXTREME = "EXTREME"

    @property
    def rank(self) -> int:
        """Orden para comparar si un estado escala respecto a otro."""
        return _STATE_ORDER[self]


_STATE_ORDER = {
    State.NORMAL: 0,
    State.WATCH: 1,
    State.HOT: 2,
    State.SIGNAL: 3,
    State.EXTREME: 4,
}


@dataclass(frozen=True)
class Candle:
    """Vela de 1 minuto. `ts` es el timestamp de apertura en milisegundos."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    base_vol: float
    quote_vol: float

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3


@dataclass(frozen=True)
class Contract:
    symbol: str
    base_coin: str
    symbol_type: str
    status: str
    is_rwa: bool


@dataclass(frozen=True)
class Ticker:
    symbol: str
    last: float
    change_24h: float  # en porcentaje, ya convertido desde la fracción
    volume_24h_usdt: float
    open_interest: float
    funding_rate: float
    ts: int
