"""Levanta SOLO el dashboard contra una base ya existente, sin conectarse a
Bitget ni recolectar nada.

Para mirar el panel del bot con datos reales sin arrancar el escaner entero
(que tira de la API publica, rellena velas y tarda). El escaner sale vacio
-no hay ningun `Orchestrator` vivo detras-; lo que se ve de verdad es la
seccion BOT.

    .venv/bin/python ver_panel.py [ruta/a/la.db] [puerto]
"""
import sys
from pathlib import Path

import uvicorn

from scanner_volumen.api.server import create_app
from scanner_volumen.app.state import ScannerState
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import SignalRepo

db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/panel_local.db")
puerto = int(sys.argv[2]) if len(sys.argv) > 2 else 8002
conn = open_db(db)
repo = BotRepo(conn)
print(f"  base   : {db}")
print(f"  trades : {len(repo.cerradas('paper'))} cerrados, "
      f"{len(repo.abiertas('paper'))} abiertos")
print(f"  equity : {repo.equity('paper'):.2f}")
print(f"\n  -> http://127.0.0.1:{puerto}\n")
app = create_app(ScannerState(), SignalRepo(conn), bot_repo=repo, modo="paper")
uvicorn.run(app, host="127.0.0.1", port=puerto, log_level="warning")
