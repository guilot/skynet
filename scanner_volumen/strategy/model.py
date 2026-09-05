"""Tipos de la estrategia, compartidos por el backtest y el bot en vivo.

Sin lógica: dataclasses y enum. Vive fuera de `backtest/` porque el bot en
vivo (Fase 2) los consume igual que el backtest, y `strategy` no puede
depender de `backtest` (ver el spec, invariante de dependencias).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from scanner_volumen.models import Direction, State

MIN_MS = 60_000


class ExitReason(str, Enum):
    SCALE_HOT = "SCALE_HOT"        # tramo 33% al cruzar >= HOT (regla 3)
    SCALE_SIGNAL = "SCALE_SIGNAL"  # tramo 33% al cruzar >= SIGNAL (regla 4)
    EXTREME = "EXTREME"            # cierre total al cruzar EXTREME (regla 5)
    STOP = "STOP"                  # stop de precio -stop_pct (salida 2)
    STALE_BE = "STALE_BE"          # 10 min sin cambio de estado: salida limitada en BE
    END_OF_DATA = "END_OF_DATA"    # abierta al agotarse las velas


@dataclass(frozen=True)
class StrategyParams:
    equity_inicial: float = 1000.0
    fraccion_margen: float = 0.02
    apalancamiento: float = 20.0
    comision_taker: float = 0.0006
    stop_pct: float = 0.025
    max_concurrentes: int = 5
    stale_min: int = 10
    tramo_hot: float = 0.33
    tramo_signal: float = 0.33
    min_score_entrada: float = 70.0  # score mínimo de la entrada (solo HOT+ en la práctica)
    extreme_run_min: float = 3.0    # min a mantener el resto tras EXTREME (0 = cerrar ya)
    freeze_perdidas: int = 3        # nº de pérdidas seguidas por par que congelan (0 = off)
    freeze_ventana_horas: float = 1.0  # ventana en la que deben caer esas pérdidas
    freeze_horas: float = 3.0       # cuánto se congela el par tras dispararse


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
    """Una salida ya ejecutada, con el precio REAL obtenido."""

    ts: int
    price: float
    fraction: float
    reason: ExitReason


@dataclass(frozen=True)
class ExitIntent:
    """Una salida que las reglas proponen pero que aún no se ha ejecutado.

    `precio_referencia` es el precio que la regla considera justo (el que el
    backtest usa tal cual); en vivo el broker devolverá otro y el motor se
    entera por `PositionRules.on_fill`.
    """

    ts: int
    fraction: float
    reason: ExitReason
    precio_referencia: float


@dataclass(frozen=True)
class PositionOutcome:
    symbol: str
    direction: Direction
    entry_ts: int
    entry_price: float
    fills: tuple[Fill, ...]
    max_rank: int
    close_ts: int


@dataclass(frozen=True)
class TradeResumen:
    """Un trade cerrado, en la forma mínima que el informe necesita.

    La construyen tanto el backtest (desde su `ClosedTrade`) como el bot (desde
    su base de datos), para que un único formateador sirva a los dos.
    """

    symbol: str
    direction: Direction
    entry_ts: int
    entry_price: float
    close_ts: int
    fills: tuple[Fill, ...]
    fill_pnls: tuple[float, ...]
    margin: float
    pnl: float
    fees: float
    max_rank: int


@dataclass(frozen=True)
class ResumenOperativa:
    """Todo lo que el informe necesita, sin saber quién operó.

    `descartes` es un diccionario ordenado etiqueta -> cuenta: el orden de
    inserción es el orden de impresión, y cada productor mete las etiquetas que
    le aplican (el backtest tiene "sin velas", el bot tiene "desvio").
    """

    titulo: str
    trades: tuple[TradeResumen, ...]
    descartes: dict[str, int]
    equity_inicial: float
    equity_final: float
    ts_min: int | None
    ts_max: int | None
    total_transiciones: int
    max_concurrentes_alcanzado: int
