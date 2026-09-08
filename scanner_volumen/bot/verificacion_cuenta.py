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
símbolo; las siguientes veces devuelve el resultado guardado. El cableado
(Task 13) llama a esto justo antes de la primera entrada en cada símbolo, no
en un bucle de arranque.

**Nunca modifica la cuenta.** Si la configuración no coincide con lo que la
estrategia asume, esta función avisa (log) y veta -no corrige nada en
Bitget. Cambiar el apalancamiento o el modo de margen de una cuenta por su
cuenta es justo lo que un programa no debe hacer (spec, §10); esa decisión
es de un humano, en el propio Bitget.

**Consecuencia del veto**: solo se descarta ESE símbolo, nunca se tumba el
bot -un par mal configurado no debe dejar sin operar a los demás. Reutiliza
la misma etiqueta de descarte que la reconciliación de arranque usa para una
posición ajena (`"simbolo vetado"`, ver `bot/model.py` y `BotRunner.
reconciliar_con_exchange`): desde el punto de vista del informe, los dos
motivos significan lo mismo, "este símbolo no se toca esta sesión".

Deliberadamente pura y sin red: `lector` es una dependencia inyectada (un
callable async símbolo -> `ConfiguracionCuentaSymbol`), así que estos tests
no necesitan ni `BitgetPrivate` ni httpx. La implementación real de
`lector` es `BitgetPrivate.get_configuracion_symbol` (Task 13 la conecta);
ver ahí el supuesto sin verificar sobre el endpoint y los nombres de campo.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from scanner_volumen.bitget.private import ConfiguracionCuentaSymbol
from scanner_volumen.strategy.model import StrategyParams

log = logging.getLogger(__name__)

# Misma etiqueta que `BotRunner.reconciliar_con_exchange` usa para un símbolo
# ajeno detectado al arrancar (ver `ETIQUETAS_DESCARTE` en `bot/model.py`):
# no se introduce una etiqueta nueva porque, para el informe, un símbolo
# vetado por apalancamiento y uno vetado por ser de un tercero cuentan lo
# mismo -"el bot no toca esto esta sesión"-, y una etiqueta nueva no aporta
# nada que el operador necesite distinguir desde el informe.
MOTIVO_VETO = "simbolo vetado"

LectorConfiguracionCuenta = Callable[[str], Awaitable[ConfiguracionCuentaSymbol]]


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
        el resultado cacheado -sea cual sea, incluido un veto."""
        if symbol not in self._cache:
            self._cache[symbol] = await self._verificar_sin_cache(symbol)
        return self._cache[symbol]

    async def _verificar_sin_cache(self, symbol: str) -> str | None:
        config = await self._lector(symbol)
        if not config.margen_aislado:
            log.warning(
                "bot: %s no esta en margen aislado; se veta el simbolo -- "
                "el bot NUNCA cambia la configuracion de la cuenta por su "
                "cuenta, corrigelo a mano en Bitget", symbol,
            )
            return MOTIVO_VETO
        esperado = self._params.apalancamiento
        if (config.apalancamiento_long != esperado
                or config.apalancamiento_short != esperado):
            log.warning(
                "bot: %s tiene apalancamiento long=%s short=%s, se esperaba "
                "%s; se veta el simbolo -- el bot NUNCA lo cambia por su "
                "cuenta, corrigelo a mano en Bitget",
                symbol, config.apalancamiento_long,
                config.apalancamiento_short, esperado,
            )
            return MOTIVO_VETO
        return None
