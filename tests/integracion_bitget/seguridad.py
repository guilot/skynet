"""Restricción de seguridad del banco de pruebas (Task 12): módulo separado,
no un test, precisamente para que `conftest.py` y los tests unitarios de esta
misma guarda puedan importarlo sin depender uno del otro.

Este es el único código del proyecto que manda órdenes de verdad contra un
exchange, así que tiene que ser **estructuralmente incapaz** de tocar la
cuenta real -no basta con que el llamador lo use bien, el propio banco debe
negarse-. Dos guardas, independientes entre sí:

1. Las credenciales solo pueden venir de las tres variables de entorno
   definidas aquí abajo, propias de demo. No existe ningún camino de código
   en este banco que lea otro nombre de variable, así que no hay fallback
   posible a unas claves de producción -ni siquiera un fallback accidental,
   porque no se busca nada más-.
2. `_verificar_entorno_de_simulacion` se llama antes de construir el cliente
   autenticado (ver `conftest.py`) y ABORTA si el `productType` o el símbolo
   no son inequívocamente de simulación. Recibe los valores explícitos en
   vez de leer las constantes de este módulo directamente, para que un test
   que la ejercite pueda demostrar que de verdad discrimina (ver
   `test_seguridad.py`) y no sea una comprobación tautológica.
"""
from __future__ import annotations

# Nombres de variable EXCLUSIVOS de demo. Nunca los de producción -que hoy ni
# siquiera existen como convención en el proyecto (la Task 13, que cablea el
# modo real, es quien las definirá)-, y aunque existieran, este módulo no las
# lee bajo ningún nombre distinto de estos tres.
VAR_KEY = "SCANNER_BITGET_DEMO_KEY"
VAR_SECRET = "SCANNER_BITGET_DEMO_SECRET"
VAR_PASSPHRASE = "SCANNER_BITGET_DEMO_PASSPHRASE"

# Hardcodeados a propósito: ni argumento de CLI, ni variable de entorno, ni
# parámetro de fixture. El banco de pruebas no puede apuntar a otro
# `productType` ni a otro símbolo aunque alguien lo intente.
PRODUCT_TYPE_DEMO = "SUSDT-FUTURES"
SYMBOL_DEMO = "SBTCSUSDT"

# El discriminante de verdad es el SUFIJO, no el prefijo. Los símbolos de
# simulación cotizan contra SUSDT (`SBTCSUSDT`, `SETHSUSDT`), mientras que
# el prefijo "S" lo cumplen un montón de símbolos REALES que este mismo
# escáner opera -SOLUSDT, SUIUSDT, SHIBUSDT, SEIUSDT, SANDUSDT, SXPUSDT-.
# Comprobar solo el prefijo dejaba pasar los seis: ver `test_seguridad.py`,
# donde están fijados uno a uno como casos negativos.
SUFIJO_DEMO = "SUSDT"


def _verificar_entorno_de_simulacion(venue: str, symbol: str) -> None:
    """Aborta (con `RuntimeError`, no con un `assert` que `-O` podría
    silenciar) si `venue` o `symbol` no son inequívocamente de la cuenta de
    simulación de Bitget.

    Se llama SIEMPRE antes de construir el cliente autenticado, en la
    fixture `privado` de `conftest.py` -no depende de que cada test se
    acuerde de comprobarlo-.
    """
    if venue != PRODUCT_TYPE_DEMO:
        raise RuntimeError(
            f"ABORTADO por seguridad: productType={venue!r} no es el de "
            f"simulacion de Bitget ({PRODUCT_TYPE_DEMO!r}). Este banco de "
            f"pruebas se NIEGA a construir un cliente autenticado que no "
            f"apunte, de forma inequivoca, al entorno de demo trading."
        )
    if not (symbol.startswith("S") and symbol.endswith(SUFIJO_DEMO)):
        raise RuntimeError(
            f"ABORTADO por seguridad: symbol={symbol!r} no es de "
            f"simulacion. Los simbolos de demo empiezan por 'S' Y cotizan "
            f"contra {SUFIJO_DEMO} (p.ej. 'SBTCSUSDT'). El sufijo es el "
            f"discriminante que importa: hay simbolos REALES que empiezan "
            f"por 'S' (SOLUSDT, SUIUSDT, SHIBUSDT...) y comprobar solo el "
            f"prefijo los dejaba pasar. Este banco de pruebas se NIEGA a "
            f"operar un simbolo que podria coincidir con uno real."
        )
