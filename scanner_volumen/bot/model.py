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


@dataclass(frozen=True)
class PosicionExchange:
    """Una posición tal como la reporta el exchange, ya traducida al
    vocabulario del bot (símbolo, dirección, tamaño en unidades del activo).

    La reconciliación de arranque (Task 8, `BotRunner.reconciliar_con_
    exchange`) la compara contra lo que el bot cree tener abierto en su
    base de datos -la verdad, en modo real, la tiene el exchange, no la
    base-. `client_oid`, si se conoce, es el identificador de la orden que
    la abrió: es lo que permite reconocer como propia una posición que un
    proceso murió a medias sin llegar a confirmar (ver `BotRepo.
    reservadas_sin_confirmar`); puede ser `None` si no se pudo recuperar
    (p. ej. porque el exchange ya no conserva ese historial de órdenes).

    `fee_entrada`, si se conoce, es la comisión que el exchange cobró de
    verdad al abrir -la reconciliación la usa para sustituir el valor
    provisional de una reserva confirmada por `client_oid` (ver
    `BotRunner._resolver_reserva`); sin ella, ese PnL queda optimista en
    exactamente lo que costó abrir. Puede ser `None` si el exchange no la
    conserva junto al resto de datos de la posición.
    """

    symbol: str
    direction: Direction
    size: float
    entry_price: float
    entry_ts: int
    client_oid: str | None = None
    fee_entrada: float | None = None


# Etiquetas de descarte del bot, en el orden en que se imprimen. Coinciden con
# las del backtest salvo "sin velas" (que en vivo no aplica) y "desvio" (que en
# el backtest no existe). "simbolo vetado" es exclusiva de la Fase 3: la
# reconciliación de arranque (Task 8) veta un símbolo que el exchange tiene
# abierto y el bot no reconoce, para no volver a tocarlo en toda la sesión.
# "config cuenta" (Task 11, `bot/verificacion_cuenta.py`) es una etiqueta
# DISTINTA a propósito, aunque las dos "veten" un símbolo para el resto de
# la sesión: una posición ajena y un apalancamiento mal configurado piden
# ACCIONES OPUESTAS del operador, y mezclarlas en un solo número le
# esconde cuál de las dos tiene que tomar.
# "perdida diaria" y "parada de emergencia" son los frenos manuales de la
# Fase 3 (Task 10, `bot/frenos.py`): a diferencia de las demás etiquetas, que
# se contabilizan transición a transición, estas cuentan una vez por tick en
# el que el freno impidió evaluar entradas.
# La etiqueta del veto por configuracion de cuenta vive AQUI y no en
# `bot/verificacion_cuenta.py`, que es quien la produce, porque tambien la
# necesita el informe -y ese modulo arrastra `bitget/private.py` y con el
# `httpx`. El CLI del informe es SOLO LECTURA y no toca la red: hacerle
# depender del cliente HTTP para leer una cadena de texto rompia su
# capacidad de correr en un entorno minimo (paso de verdad: `python3 -m
# scanner_volumen.bot` reventaba con ModuleNotFoundError: httpx).
MOTIVO_CONFIG_CUENTA = "config cuenta"

ETIQUETAS_DESCARTE = (
    "NEUTRAL",
    "simbolo abierto",
    "tope concurrencia",
    "score bajo",
    "par congelado",
    "desvio",
    "simbolo vetado",
    MOTIVO_CONFIG_CUENTA,
    "perdida diaria",
    "parada de emergencia",
)
