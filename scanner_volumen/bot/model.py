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
    # Identificador del stop vigente en el exchange (o su simulacro en
    # paper), o `None` si todavía no se ha colocado ninguno -o si el intento
    # de colocarlo agotó los reintentos y la posición se cerró a mercado
    # antes de que existiera-. `mover_stop` lo sustituye por uno nuevo cada
    # vez que se mueve (en un exchange real mover un stop es cancelar el
    # viejo y colocar otro, ver `bot.broker`), así que este campo siempre
    # apunta al stop realmente vivo, no al primero que se colocó.
    stop_id: str | None = None
    # El nivel de `reglas.stop_price` en el momento en que `stop_id` se
    # colocó o se movió por última vez. El runner lo compara en cada tick
    # contra `reglas.stop_price` para detectar que la regla movió el stop
    # (hoy, únicamente la subida a break-even) y reflejarlo en el exchange;
    # sin guardarlo aquí no habría forma de distinguir "la regla lo movió
    # este tick" de "sigue igual que siempre".
    stop_price_colocado: float | None = None


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
