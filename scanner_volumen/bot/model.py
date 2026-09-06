"""Tipos del bot de ejecución."""
from __future__ import annotations

from dataclasses import dataclass

from scanner_volumen.models import Direction
from scanner_volumen.strategy.position import PositionRules


@dataclass(frozen=True)
class OrdenEjecutada:
    """El resultado real de mandar una orden: lo que el mercado dio."""

    ts: int
    precio: float
    cantidad: float
    comision: float


@dataclass
class PosicionAbierta:
    """Una posición viva: su fila en la base de datos más el motor que la gobierna.

    `entry_price` es el precio EJECUTADO (al que se ancla el stop, el
    break-even y el estancamiento); `entry_price_senal` es el de la señal, y se
    guarda solo para medir el desvío de entrada.
    """

    id: int
    symbol: str
    direction: Direction
    entry_ts: int
    entry_price: float
    entry_price_senal: float
    margin: float
    notional: float
    size: float
    reglas: PositionRules
    pnl_acumulado: float = 0.0
    fees_acumuladas: float = 0.0
    # True si un fallo del broker dejó una intención de salida sin confirmar y
    # el runner tuvo que aislarla para no reventar en el siguiente tick. Sigue
    # en `abiertas` y ocupa su hueco de concurrencia porque sigue realmente
    # abierta en la base de datos (`abierta = 1`); es la reconstrucción al
    # reiniciar (Task 7) la que la recupera, no este proceso en caliente.
    degradada: bool = False


# Etiquetas de descarte del bot, en el orden en que se imprimen. Coinciden con
# las del backtest salvo "sin velas" (que en vivo no aplica) y "desvio" (que en
# el backtest no existe).
ETIQUETAS_DESCARTE = (
    "NEUTRAL",
    "simbolo abierto",
    "tope concurrencia",
    "score bajo",
    "par congelado",
    "desvio",
)
