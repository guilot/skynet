"""Cartera del bot en vivo: quién entra, con cuánto margen y quién queda vetado.

Reutiliza los predicados de `strategy/entries.py` para que las reglas de
entrada sean literalmente las mismas que las del backtest. Lo que NO se comparte
es el bucle: el backtest pre-simula todos los resultados y luego los reordena
por instante de cierre; aquí el tiempo avanza de verdad y no hay nada que
pre-simular.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable

from scanner_volumen.bot.model import ETIQUETAS_DESCARTE
from scanner_volumen.bot.modo import PAPER
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.config import BotConfig
from scanner_volumen.models import Direction
from scanner_volumen.strategy.entries import FreezeTracker, score_suficiente
from scanner_volumen.strategy.model import StrategyParams, TransitionRow

log = logging.getLogger(__name__)


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
        # Solo se avisa UNA vez por instancia del cableado sospechoso "modo
        # real sin proveedor de saldo" (ver `equity`): esta llamada puede
        # hacerse en cada tick, y repetir el mismo `log.error` en cada uno
        # ahogaría el log sin añadir información nueva.
        self._aviso_sin_proveedor_emitido = False

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
        `margen`, más abajo, para el razonamiento completo).

        VALIDA lo que devuelve `proveedor_saldo` (hallazgo de revisión): un
        valor no finito o no positivo (`nan`, `inf`, negativo, cero) no
        puede dimensionar nada -antes de esta validación, un `-300` se
        propagaba tal cual hasta un margen negativo, un tamaño de posición
        negativo, y una orden real mandada sin ninguna guarda. Se lanza
        `ValueError` en vez de devolver algo.

        OJO CON QUÉ HACE QUIEN LLAMA A ESTO (corregido tras un hallazgo de
        revisión posterior, que demostró que la frase anterior aquí era
        FALSA en general): esta función no controla ni garantiza cómo se
        aísla su propia excepción -eso depende de cada llamador-. En el
        bucle de entradas de `on_tick` sí es cierto que cada entrada está
        aislada en su propio `try/except`, así que ahí un fallo descarta
        solo esa entrada. Pero `equity()` tiene OTRO llamador con una
        isolación muy distinta: el log informativo de `BotRunner._cerrar`
        tras cerrar una posición. Antes de que ese call site se corrigiera
        también en esta ronda, un fallo aquí se propagaba hasta el
        `try/except` que gobierna `self.abiertas` en `on_tick` y marcaba
        `degradada` una posición que YA estaba cerrada -un estado sin
        sentido que le mentía al operador en el informe-, justo lo
        contrario de "descarta solo esa entrada". `_cerrar` ya aísla ese
        fallo localmente, pero la lección se queda documentada aquí:
        cualquier llamador NUEVO de `equity()` tiene que decidir
        explícitamente qué hacer si lanza, no asumir que "se aísla solo"
        porque otro llamador lo hace.

        AVISA si el modo es real y no hay `proveedor_saldo` (una sola vez
        por instancia): es el error de cableado más probable de la Task 13
        -sin él, el margen se dimensiona sobre el equity CONTABLE incluso
        en real, exactamente lo que este step existe para evitar- y hoy no
        dejaba ninguna señal."""
        if self._proveedor_saldo is not None:
            saldo = self._proveedor_saldo()
            if not math.isfinite(saldo) or saldo <= 0:
                raise ValueError(
                    f"proveedor_saldo devolvio un saldo invalido para "
                    f"dimensionar el margen: {saldo!r} (se esperaba un "
                    f"numero finito y positivo)"
                )
            return saldo
        if self._cfg.modo != PAPER and not self._aviso_sin_proveedor_emitido:
            log.error(
                "bot: modo %s sin proveedor_saldo inyectado; el margen se "
                "esta dimensionando sobre el equity CONTABLE de la base, "
                "NO sobre el saldo real del exchange -- revisar el "
                "cableado (Task 13)", self._cfg.modo,
            )
            self._aviso_sin_proveedor_emitido = True
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
