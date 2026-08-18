"""Apertura de la base de datos en modo solo-lectura.

El scanner puede seguir corriendo y escribiendo en la misma base mientras se
ejecuta un backtest (spec: "Do not modify that database"). `open_db` de
`storage/db.py` no sirve aquí: aplica migraciones y `executescript(ESQUEMA)`,
escrituras que esta herramienta no necesita y que no debe arriesgarse a
hacer contra una base en uso. Se abre con el URI `mode=ro` de SQLite, que
hace que cualquier intento de escritura falle en el propio driver -no solo
"no se escribe por convención", sino que no puede escribirse-.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def open_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"no existe la base de datos: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn
