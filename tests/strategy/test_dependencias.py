"""El bot en vivo (Fase 2) consumirá `strategy/` sin arrastrar el backtest.

Un import accidental de `scanner_volumen.backtest` desde `strategy/` o `bot/`
haría que el bot dependiera de la maquinaria de backtesting (sqlite de solo
lectura, informes, CLI). Se comprueba sobre el árbol de sintaxis en vez de
importando, para que el fallo señale el fichero y la línea exactos.
"""
from __future__ import annotations

import ast
from pathlib import Path

PAQUETES = {
    "strategy": Path(__file__).resolve().parents[2] / "scanner_volumen" / "strategy",
    "bot": Path(__file__).resolve().parents[2] / "scanner_volumen" / "bot",
}

# Umbral real de ficheros .py por paquete (contados a mano: 5 en strategy/,
# 8 en bot/), no un ">= 1" simbólico: si el paquete perdiera ficheros de
# golpe (p. ej. se movieran de sitio) el umbral debe notarlo, no limitarse a
# comprobar que queda alguno.
UMBRAL_MINIMO_FICHEROS = {"strategy": 5, "bot": 8}


def _imports(fichero: Path) -> list[str]:
    arbol = ast.parse(fichero.read_text(encoding="utf-8"), filename=str(fichero))
    nombres: list[str] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            nombres.extend(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            nombres.append(nodo.module)
    return nombres


def test_ni_strategy_ni_bot_importan_backtest():
    """El bot en producción no puede arrastrar la maquinaria de backtesting:
    SQLite de solo lectura, informes de simulación, su CLI. La flecha va
    backtest -> strategy y bot -> strategy, nunca al revés."""
    ofensores: list[str] = []
    for nombre, raiz in PAQUETES.items():
        for fichero in sorted(raiz.rglob("*.py")):
            for modulo in _imports(fichero):
                if modulo.startswith("scanner_volumen.backtest"):
                    ofensores.append(f"{nombre}/{fichero.relative_to(raiz)} importa {modulo}")
    assert not ofensores, (
        f"paquetes compartidos dependiendo del backtest: {ofensores}"
    )


def test_hay_ficheros_que_comprobar():
    # si los paquetes se movieran de sitio, los tests anteriores pasarían en vacío
    for nombre, raiz in PAQUETES.items():
        ficheros = list(raiz.rglob("*.py"))
        umbral = UMBRAL_MINIMO_FICHEROS[nombre]
        assert len(ficheros) >= umbral, (
            f"solo {len(ficheros)} ficheros en {nombre}/ (se esperaban al "
            f"menos {umbral}); revisa si el paquete se movió de sitio"
        )
