"""Tipos del backtest de trayectoria. Sin lógica: dataclasses y enum."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from scanner_volumen.models import Direction, State


class ExitReason(str, Enum):
    SCALE_HOT = "SCALE_HOT"        # tramo 33% al cruzar >= HOT (regla 3)
    SCALE_SIGNAL = "SCALE_SIGNAL"  # tramo 33% al cruzar >= SIGNAL (regla 4)
    EXTREME = "EXTREME"            # cierre total al cruzar EXTREME (regla 5)
    STOP = "STOP"                  # stop de precio -stop_pct (salida 2)
    STALE_BE = "STALE_BE"          # 10 min sin cambio de estado: salida limitada en BE
    END_OF_DATA = "END_OF_DATA"    # abierta al agotarse las velas


@dataclass(frozen=True)
class TrajectoryParams:
    equity_inicial: float = 1000.0
    fraccion_margen: float = 0.02
    apalancamiento: float = 20.0
    comision_taker: float = 0.0006
    stop_pct: float = 0.025
    max_concurrentes: int = 5
    stale_min: int = 10
    tramo_hot: float = 0.33
    tramo_signal: float = 0.33
    min_score_entrada: float = 0.0  # score mínimo del cruce a WATCH para entrar
    extreme_run_min: float = 3.0    # min a mantener el resto tras EXTREME (0 = cerrar ya)


@dataclass(frozen=True)
class TransitionRow:
    ts: int
    symbol: str
    prev_state: State
    new_state: State
    price: float
    direction: Direction
    score: float


@dataclass(frozen=True)
class CandleRow:
    ts: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Fill:
    ts: int
    price: float
    fraction: float
    reason: ExitReason


@dataclass(frozen=True)
class PositionOutcome:
    symbol: str
    direction: Direction
    entry_ts: int
    entry_price: float
    fills: tuple[Fill, ...]
    max_rank: int
    close_ts: int
