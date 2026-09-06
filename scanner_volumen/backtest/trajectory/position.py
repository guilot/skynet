"""Driver offline del motor de reglas.

Recorre las velas históricas de UN símbolo alimentando `PositionRules` y
ejecuta cada intención al precio de referencia y en el acto -que es lo que
significa "ejecutar" cuando los datos ya ocurrieron-. Toda la lógica de
decisión vive en `scanner_volumen.strategy.position`; aquí solo queda el
recorrido y el cierre por fin de datos, que es una condición del backtest y
no de la estrategia.

El resultado es independiente del margen: los `fills` son fracciones del
tamaño original y `portfolio.py` les aplica el dinero después.
"""
from __future__ import annotations

from scanner_volumen.strategy.model import (
    CandleRow, ExitReason, Fill, MIN_MS, PositionOutcome, StrategyParams, TransitionRow,
)
from scanner_volumen.strategy.position import PositionRules, agrupar_por_vela

__all__ = ["MIN_MS", "simulate_position"]


def simulate_position(
    entry: TransitionRow,
    later: list[TransitionRow],
    candles: list[CandleRow],
    params: StrategyParams,
) -> PositionOutcome:
    if not candles:
        # No debería alcanzarse en producción: run_trajectory filtra antes las
        # entradas sin cobertura de velas (retención). Este guard convierte
        # cualquier violación futura del contrato en un error explícito en vez
        # de un IndexError silencioso más abajo.
        raise ValueError("simulate_position requiere al menos una vela")

    rules = PositionRules(entry, params)
    por_vela = agrupar_por_vela(later, candles)
    fills: list[Fill] = []

    for c in candles:
        if rules.cerrada:
            break
        for intent in rules.on_candle(c, por_vela.get(c.ts, ())):
            # offline "ejecutar" es tomar el precio de referencia sin demora
            fill = Fill(ts=intent.ts, price=intent.precio_referencia,
                        fraction=intent.fraction, reason=intent.reason)
            rules.on_fill(fill)
            fills.append(fill)

    if not rules.cerrada:
        ultima = candles[-1]
        fills.append(Fill(ts=ultima.ts, price=ultima.close,
                          fraction=rules.restante, reason=ExitReason.END_OF_DATA))

    return PositionOutcome(
        symbol=entry.symbol, direction=entry.direction, entry_ts=entry.ts,
        entry_price=entry.price, fills=tuple(fills), max_rank=rules.max_rank,
        close_ts=fills[-1].ts,
    )
