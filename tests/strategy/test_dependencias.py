"""El bot en vivo (Fase 2) consumirá `strategy/` sin arrastrar el backtest.

Un import accidental de `scanner_volumen.backtest` desde `strategy/` haría
que el bot dependiera de la maquinaria de backtesting (sqlite de solo
lectura, informes, CLI). Se comprueba sobre el árbol de sintaxis en vez de
importando, para que el fallo señale el fichero y la línea exactos.
"""
from __future__ import annotations

import ast
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2] / "scanner_volumen" / "strategy"


def _imports(fichero: Path) -> list[str]:
    arbol = ast.parse(fichero.read_text(encoding="utf-8"), filename=str(fichero))
    nombres: list[str] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            nombres.extend(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            nombres.append(nodo.module)
    return nombres


def test_strategy_no_importa_backtest():
    ofensores: list[str] = []
    for fichero in sorted(RAIZ.rglob("*.py")):
        for modulo in _imports(fichero):
            if modulo.startswith("scanner_volumen.backtest"):
                ofensores.append(f"{fichero.name} importa {modulo}")
    assert not ofensores, (
        "strategy/ no puede depender de backtest/ (la flecha va "
        f"backtest -> strategy): {ofensores}"
    )


def test_hay_ficheros_que_comprobar():
    # si el paquete se moviera de sitio, el test anterior pasaría en vacío
    assert len(list(RAIZ.rglob("*.py"))) >= 4
