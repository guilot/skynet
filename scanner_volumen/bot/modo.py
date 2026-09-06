"""Resuelve el modo efectivo del bot a partir de la config y del entorno.

El interruptor de dinero real necesita DOS llaves: `modo = "real"` en el fichero
de configuración y una variable de entorno en el servicio. Así un `git pull` que
traiga por error una config con `real`, o un despliegue descuidado, no pueden
poner dinero de verdad en juego: la segunda llave vive fuera del repositorio.

Una sola variable con tres estados, en vez de dos banderas que puedan
contradecirse entre sí. Y falla cerrado: cualquier valor que no sea exactamente
uno de los previstos deja el proceso sin arrancar, nunca en modo real.
"""
from __future__ import annotations

from collections.abc import Mapping

from scanner_volumen.config import BotConfig

PAPER = "paper"
REAL_LECTURA = "real_lectura"
REAL = "real"

VARIABLE = "SCANNER_BOT_REAL"
_VALORES = {"lectura": REAL_LECTURA, "ordenes": REAL}


def resolver_modo(cfg_bot: BotConfig, entorno: Mapping[str, str]) -> str:
    """Modo efectivo, que es el que se persiste en `bot_posiciones.modo`.

    `paper` ignora el entorno por completo. `real` exige la variable, y su valor
    decide si además se sueltan las órdenes reales."""
    if cfg_bot.modo == PAPER:
        return PAPER

    valor = entorno.get(VARIABLE)
    if valor not in _VALORES:
        raise ValueError(
            f"bot.modo = 'real' exige la variable de entorno {VARIABLE} con "
            f"valor 'lectura' (conecta con Bitget pero ejecuta en paper) u "
            f"'ordenes' (manda ordenes reales). Llego: {valor!r}"
        )
    return _VALORES[valor]
