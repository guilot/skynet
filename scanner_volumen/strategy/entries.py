"""Reglas de entrada de la estrategia, compartidas por backtest y bot.

Deciden si una transición es candidata a abrir posición y si un par está
castigado por una racha de pérdidas. Lo que NO vive aquí es el bucle de
cartera (concurrencia, margen, compounding): offline pre-simula todos los
resultados y luego los reordena, y en vivo el tiempo avanza de verdad. Son
dos algoritmos distintos que comparten estas reglas.
"""
from __future__ import annotations

from scanner_volumen.models import State
from scanner_volumen.strategy.model import StrategyParams, TransitionRow

_WATCH = State.WATCH.rank
HORA_MS = 3_600_000


def es_entrada(t: TransitionRow) -> bool:
    """True si la transición CRUZA hacia WATCH o más desde por debajo."""
    return t.prev_state.rank < _WATCH <= t.new_state.rank


def score_suficiente(t: TransitionRow, params: StrategyParams) -> bool:
    return t.score >= params.min_score_entrada


class FreezeTracker:
    """Congela un par tras `freeze_perdidas` cierres en pérdida consecutivos
    dentro de `freeze_ventana_horas`. Con `freeze_perdidas <= 0` no hace nada.
    """

    def __init__(self, params: StrategyParams) -> None:
        self._perdidas = params.freeze_perdidas
        self._ventana_ms = int(params.freeze_ventana_horas * HORA_MS)
        self._congelar_ms = int(params.freeze_horas * HORA_MS)
        self._racha: dict[str, list[int]] = {}
        self._hasta: dict[str, int] = {}

    def congelado(self, symbol: str, ts: int) -> bool:
        return self._hasta.get(symbol, 0) > ts

    def registrar(self, symbol: str, close_ts: int, pnl: float) -> None:
        if self._perdidas <= 0:
            return
        if pnl >= 0:
            self._racha[symbol] = []  # un no-perdedor rompe la racha
            return
        racha = self._racha.setdefault(symbol, [])
        racha.append(close_ts)
        del racha[:-self._perdidas]  # conserva solo las últimas N
        if len(racha) >= self._perdidas and racha[-1] - racha[0] <= self._ventana_ms:
            self._hasta[symbol] = close_ts + self._congelar_ms
            racha.clear()
