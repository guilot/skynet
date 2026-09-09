"""Verificación perezosa, por símbolo, de la configuración de margen y
apalancamiento en Bitget (Task 11).

**Por qué por símbolo y no "de la cuenta"** (corrección del plan original,
ver `progress.md`): en Bitget el apalancamiento no es una propiedad de la
cuenta, sino de cada combinación symbol + marginCoin + holdSide. El bot
opera cualquier par que dé señal, y muchos de esos pares no se conocen hasta
que aparecen en el escáner -no existe, por tanto, un instante de arranque en
el que se pueda verificar "la cuenta" entera. La verificación se hace aquí,
símbolo a símbolo, la primera vez que ese símbolo pide una entrada.

**Perezosa y cacheada**: `VerificadorCuenta.verificar(symbol)` solo consulta
el exchange (a través de `lector`) la primera vez que se le pregunta por un
símbolo; las siguientes veces devuelve el resultado guardado -sea un veto o
no-, SALVO si `lector` lanzó una excepción: un fallo transitorio (red,
límite de peticiones, un 5xx puntual de Bitget) NO se cachea, así que la
siguiente consulta reintenta de verdad en vez de quedar vetada para
siempre por un error que ya pasó (ver `verificar`). El cableado (Task 13)
llama a esto justo antes de la primera entrada en cada símbolo, no en un
bucle de arranque.

**Nunca modifica la cuenta.** Si la configuración no coincide con lo que la
estrategia asume, esta función avisa (log) y veta -no corrige nada en
Bitget. Cambiar el apalancamiento o el modo de margen de una cuenta por su
cuenta es justo lo que un programa no debe hacer (spec, §10); esa decisión
es de un humano, en el propio Bitget. El veto dura toda la sesión del
proceso (el caché no expira): corregir la configuración en Bitget no basta
para que el bot vuelva a operar ese símbolo, hace falta además reiniciar el
proceso -se avisa de las dos cosas en el log, no solo de la primera.

**Consecuencia del veto**: solo se descarta ESE símbolo, nunca se tumba el
bot -un par mal configurado no debe dejar sin operar a los demás. Usa su
PROPIA etiqueta de descarte, `MOTIVO_VETO` ("config cuenta"), añadida a
`ETIQUETAS_DESCARTE` en `bot/model.py` -NO la misma que usa la
reconciliación de arranque para una posición ajena ("simbolo vetado").
Se consideró reutilizar esa, pero son dos causas que piden ACCIONES
OPUESTAS del operador: una posición ajena pide investigar de quién es esa
posición y decidir si tocarla a mano; un apalancamiento mal configurado
pide corregir la configuración de ESE símbolo en Bitget y reiniciar el bot.
Mezclarlas en un solo número le esconde al operador cuál de las dos
acciones tiene que tomar.

**CONTRATO CON LA TASK 13**: `verificar(symbol)` solo devuelve el motivo de
veto -no incrementa ningún contador ni toca `BotRepo`, porque esta clase se
diseñó deliberadamente sin acceso al repositorio (pura y testeable sin red,
según pedía el brief). Es responsabilidad de quien cablea esto en
`BotRunner.on_tick`: cuando `verificar(symbol)` devuelva `MOTIVO_VETO` para
una transición que se iba a evaluar, debe llamar a
`repo.incrementar_contador(modo, MOTIVO_VETO)` -una vez por transición
descartada por esta causa, igual que `LivePortfolio.evaluar_entrada` hace
con sus propios descartes, NO una vez por símbolo- para que el informe
(`format_bloque_ejecucion`) refleje algo más que una traza de log.

Deliberadamente pura y sin red: `lector` es una dependencia inyectada (un
callable async símbolo -> `ConfiguracionCuentaSymbol`), así que estos tests
no necesitan ni `BitgetPrivate` ni httpx. La implementación real de
`lector` es `BitgetPrivate.get_configuracion_symbol` (Task 13 la conecta);
ver ahí el supuesto sin verificar sobre el endpoint y los nombres de campo.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable

from scanner_volumen.bitget.private import ConfiguracionCuentaSymbol
from scanner_volumen.strategy.model import StrategyParams

log = logging.getLogger(__name__)

# Etiqueta PROPIA (no la "simbolo vetado" de la reconciliación de arranque,
# ver el docstring del módulo para por qué): debe estar también en
# `ETIQUETAS_DESCARTE` (`bot/model.py`) para que `bot/__main__.py` no la
# filtre fuera del informe.
MOTIVO_VETO = "config cuenta"

LectorConfiguracionCuenta = Callable[[str], Awaitable[ConfiguracionCuentaSymbol]]

# Tolerancia para comparar apalancamientos en punto flotante: vienen de
# cadenas JSON parseadas con `float()` (ver `BitgetPrivate.
# get_configuracion_symbol`), y una comparación `!=` exacta es la
# comparación equivocada para decidir si se opera un símbolo -un
# redondeo de representación no debe vetarlo.
_TOLERANCIA_APALANCAMIENTO = 1e-9


def _no_coincide(valor: float, esperado: float) -> bool:
    return not math.isclose(valor, esperado, rel_tol=_TOLERANCIA_APALANCAMIENTO,
                            abs_tol=_TOLERANCIA_APALANCAMIENTO)


class VerificadorCuenta:
    """Verifica, símbolo a símbolo y de forma perezosa, que la cuenta está en
    margen AISLADO al apalancamiento que `StrategyParams.apalancamiento`
    asume. Cachea el resultado por símbolo para no repetir la consulta al
    exchange en cada entrada."""

    def __init__(self, params: StrategyParams, lector: LectorConfiguracionCuenta) -> None:
        self._params = params
        self._lector = lector
        self._cache: dict[str, str | None] = {}

    async def verificar(self, symbol: str) -> str | None:
        """Motivo de veto para `symbol` (`MOTIVO_VETO`), o `None` si la
        configuración coincide con lo que la estrategia asume.

        Solo se llama a `lector` la primera vez que se pregunta por este
        símbolo en la vida de esta instancia; las siguientes, se devuelve
        el resultado cacheado -sea un veto o un `None`, y para siempre
        (reiniciar el proceso es la única forma de que un símbolo vetado
        vuelva a intentarse, ver el docstring del módulo).

        UN FALLO DE `lector` (una excepción) NO SE CACHEA: se propaga tal
        cual y la próxima llamada para el mismo símbolo vuelve a intentar
        la consulta real, en vez de quedar vetado para siempre por un
        problema transitorio (red, límite de peticiones, un error puntual
        del exchange) que ya pasó. Esto no es un caso especial en el
        código -es la consecuencia natural de escribir en `self._cache`
        SOLO después de que `_verificar_sin_cache` haya devuelto, nunca
        antes-, pero se deja constancia aquí y con un test dedicado porque
        es exactamente el tipo de comportamiento correcto por accidente
        que una refactorización futura (p. ej. "cachear antes para evitar
        peticiones duplicadas en vuelo") podría romper sin darse cuenta."""
        if symbol not in self._cache:
            self._cache[symbol] = await self._verificar_sin_cache(symbol)
        return self._cache[symbol]

    async def _verificar_sin_cache(self, symbol: str) -> str | None:
        config = await self._lector(symbol)
        # El modo de posición se comprueba PRIMERO porque es el más grave de
        # los tres: sin modo unilateral no existe `reduceOnly`, y `reduceOnly`
        # es lo que impide que una orden de cierre pueda abrir una posición
        # contraria. En `hedge_mode` Bitget rechaza todo cierre de este bot
        # con `code=40774` (observado contra la simulación en la Task 12), así
        # que el bot podría ABRIR y luego no poder cerrar -exactamente la
        # situación que esta fase entera existe para hacer imposible.
        if not config.modo_una_via:
            log.error(
                "bot: %s no esta en modo de posicion unilateral (one-way); "
                "se veta el simbolo -- sin el, las ordenes reduce-only que "
                "este bot usa para CERRAR son rechazadas por Bitget, asi que "
                "podria abrirse una posicion que luego no se puede cerrar. "
                "Cambialo a mano en Bitget (es un ajuste de cuenta, no por "
                "simbolo); el bot NUNCA lo cambia por su cuenta", symbol,
            )
            return MOTIVO_VETO
        if not config.margen_aislado:
            log.warning(
                "bot: %s no esta en margen aislado; se veta el simbolo "
                "PARA EL RESTO DE ESTA SESION -- el bot NUNCA cambia la "
                "configuracion de la cuenta por su cuenta: corrigelo a "
                "mano en Bitget y REINICIA el bot para que vuelva a "
                "intentarlo", symbol,
            )
            return MOTIVO_VETO
        esperado = self._params.apalancamiento
        if (_no_coincide(config.apalancamiento_long, esperado)
                or _no_coincide(config.apalancamiento_short, esperado)):
            log.warning(
                "bot: %s tiene apalancamiento long=%s short=%s, se esperaba "
                "%s; se veta el simbolo PARA EL RESTO DE ESTA SESION -- el "
                "bot NUNCA lo cambia por su cuenta: corrigelo a mano en "
                "Bitget y REINICIA el bot para que vuelva a intentarlo",
                symbol, config.apalancamiento_long,
                config.apalancamiento_short, esperado,
            )
            return MOTIVO_VETO
        return None
