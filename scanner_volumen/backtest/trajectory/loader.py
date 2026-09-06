"""Adaptador de la BD (solo lectura) a los tipos del backtest de trayectoria."""
from __future__ import annotations

from collections.abc import Callable

from scanner_volumen.strategy.model import CandleRow, TransitionRow
from scanner_volumen.models import Direction, State
from scanner_volumen.storage.repos import CandleRepo, StateTransitionRepo


def load_transitions(
    repo: StateTransitionRepo, desde_ms: int | None = None,
    hasta_ms: int | None = None,
) -> list[TransitionRow]:
    """`desde_ms`/`hasta_ms` acotan la ventana del backtest (ver
    `StateTransitionRepo.all_transitions`); `None` (el defecto) preserva el
    comportamiento de siempre: toda la base."""
    return [
        TransitionRow(
            ts=f["ts"], symbol=f["symbol"],
            prev_state=State(f["prev_state"]), new_state=State(f["new_state"]),
            price=f["price"], direction=Direction(f["direction"]),
            score=f["score"],
        )
        for f in repo.all_transitions(desde_ms, hasta_ms)
    ]


def make_candle_provider(
    candle_repo: CandleRepo,
) -> Callable[[str, int], list[CandleRow]]:
    def provider(symbol: str, since_ms: int) -> list[CandleRow]:
        return [
            CandleRow(ts=c.ts, open=c.open, high=c.high, low=c.low, close=c.close)
            for c in candle_repo.load(symbol, since_ms)
        ]
    return provider
