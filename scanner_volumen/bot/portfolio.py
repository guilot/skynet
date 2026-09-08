"""Cartera del bot en vivo: quién entra, con cuánto margen y quién queda vetado.

Reutiliza los predicados de `strategy/entries.py` para que las reglas de
entrada sean literalmente las mismas que las del backtest. Lo que NO se comparte
es el bucle: el backtest pre-simula todos los resultados y luego los reordena
por instante de cierre; aquí el tiempo avanza de verdad y no hay nada que
pre-simular.
"""
from __future__ import annotations

from collections.abc import Callable

from scanner_volumen.bot.model import ETIQUETAS_DESCARTE
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.config import BotConfig
from scanner_volumen.models import Direction
from scanner_volumen.strategy.entries import FreezeTracker, score_suficiente
from scanner_volumen.strategy.model import StrategyParams, TransitionRow


class LivePortfolio:
    def __init__(
        self, params: StrategyParams, cfg_bot: BotConfig, repo: BotRepo,
        proveedor_saldo: Callable[[], float] | None = None,
    ) -> None:
        """`proveedor_saldo` es opcional y por defecto `None` -el caso de
        `paper` y el de todos los tests que no necesitan tocarlo-. Cuando el
        cableado (Task 13) inyecta uno en los modos reales, `equity()` lo usa
        tal cual en vez de derivar el saldo de la base de datos: ver el
        docstring de `equity` para el porqué."""
        self._params = params
        self._cfg = cfg_bot
        self._repo = repo
        self._proveedor_saldo = proveedor_saldo
        self._freeze = FreezeTracker(params)
        self.descartes: dict[str, int] = dict.fromkeys(ETIQUETAS_DESCARTE, 0)

    # --- dinero ---

    def equity(self) -> float:
        """Saldo actual sobre el que se dimensiona el margen.

        Sin `proveedor_saldo` (siempre en `paper`, y en real hasta que la
        Task 13 lo conecte) se deriva de la base como siempre: inicial + PnL
        de lo ya cerrado. Con `proveedor_saldo` inyectado, se usa su valor TAL
        CUAL -se espera que sea el saldo YA REALIZADO de la subcuenta
        (`BitgetPrivate.get_saldo().realizado`, es decir accountEquity menos
        el PnL no realizado)-: es la única cifra que replica la semántica del
        backtest sin encogerse por el margen inmovilizado en posiciones
        abiertas ni inflarse con ganancias que todavía no existen (ver
        `margen`, más abajo, para el razonamiento completo)."""
        if self._proveedor_saldo is not None:
            return self._proveedor_saldo()
        return self._repo.equity(self._cfg.modo)

    def margen(self) -> float:
        """Margen de la próxima entrada: `fraccion_margen` del equity actual.

        No se descuenta el margen inmovilizado por las posiciones abiertas: el
        backtest calcula sobre un balance que solo se mueve al cerrar, y
        replicarlo es obligatorio para que las dos corridas sean comparables.
        Esto sigue siendo cierto con un `proveedor_saldo` real: el saldo
        REALIZADO que este usa tampoco descuenta ese margen (a diferencia del
        saldo *disponible*, que sí lo haría y por eso no se usa)."""
        return self._params.fraccion_margen * self.equity()

    # --- entradas ---

    def evaluar_entrada(
        self, t: TransitionRow, abiertos: set[str], precio_mercado: float,
    ) -> str | None:
        """`None` si la transición debe abrir posición; si no, la etiqueta del
        descarte, ya contabilizada.

        Precondición: `es_entrada(t)` es verdadero. El orden de las
        comprobaciones reproduce el del backtest, para que una transición que
        incumple varias se contabilice en la misma casilla que allí."""
        motivo = self._motivo_de_descarte(t, abiertos, precio_mercado)
        if motivo is not None:
            self.descartes[motivo] += 1
            # el contador en RAM se reinicia con el proceso; el persistido es
            # el que de verdad alimenta el informe entre arranques.
            self._repo.incrementar_contador(self._cfg.modo, motivo)
        return motivo

    def _motivo_de_descarte(
        self, t: TransitionRow, abiertos: set[str], precio_mercado: float,
    ) -> str | None:
        if t.direction is Direction.NEUTRAL:
            return "NEUTRAL"
        if not score_suficiente(t, self._params):
            return "score bajo"
        if self._freeze.congelado(t.symbol, t.ts):
            return "par congelado"
        if t.symbol in abiertos:
            return "simbolo abierto"
        if len(abiertos) >= self._params.max_concurrentes:
            return "tope concurrencia"
        if self._desvio_excesivo(t, precio_mercado):
            return "desvio"
        return None

    def _desvio_excesivo(self, t: TransitionRow, precio_mercado: float) -> bool:
        """True si el precio se ha alejado ADVERSAMENTE más del umbral.

        Solo cuenta el movimiento en contra: para un LONG, que el mercado ya
        esté por encima del precio de la señal significa perseguir la vela; que
        esté por debajo es una entrada mejor y no hay razón para rechazarla.
        Con `desvio_max_entrada = 0.0` el filtro está desactivado."""
        umbral = self._cfg.desvio_max_entrada
        if umbral <= 0 or t.price <= 0:
            return False
        adverso = (
            (precio_mercado - t.price) if t.direction is Direction.LONG
            else (t.price - precio_mercado)
        )
        return adverso > 0 and (adverso / t.price) > umbral

    # --- cierres ---

    def registrar_cierre(self, symbol: str, close_ts: int, pnl: float) -> None:
        """Alimenta la racha de pérdidas que puede congelar el par."""
        self._freeze.registrar(symbol, close_ts, pnl)
