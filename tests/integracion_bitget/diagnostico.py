"""Helper compartido por los tests de la Task 12, no un test en sí mismo.

El valor de este banco está en lo que dice CUANDO FALLA, no en que pase (así
lo pide el brief): un `RuntimeError` genérico de `_pedir` con solo el `code`/
`msg` de Bitget no basta -hay que nombrar explícitamente qué supuesto de la
lista (`task-6-report.md`, `task-11-report.md`) está bajo sospecha para ese
paso concreto, y dejar el error real de Bitget visible en el mensaje.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def bajo_sospecha(paso: str, supuestos: str) -> Iterator[None]:
    """Envuelve una llamada contra Bitget: si lanza, re-lanza como
    `AssertionError` con el supuesto señalado y el error real ENCADENADO
    (`from exc`, se conserva en la salida de pytest) en vez de dejar subir
    un `RuntimeError` desnudo que solo dice el `code` sin decir qué
    significa para este banco de pruebas."""
    try:
        yield
    except Exception as exc:
        raise AssertionError(
            f"[{paso}] Bitget devolvio un error real (ver la causa "
            f"encadenada abajo). Supuesto(s) bajo sospecha: {supuestos}. "
            f"Si el 'code'/'msg' de Bitget apunta a un parametro o campo "
            f"desconocido, ese es el supuesto que cayo -anotar la forma "
            f"real en el informe de la Task 12, NO arreglar private.py ni "
            f"bitget_broker.py desde este banco (esa es otra ronda)."
        ) from exc
