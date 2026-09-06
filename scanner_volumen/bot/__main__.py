"""CLI: `python -m scanner_volumen.bot [--db PATH] [--modo paper]`.

Abre la base de datos en solo lectura e imprime el informe del bot. Solo
lectura: mirar el informe nunca puede alterar lo que el bot lleva operado.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.bot.report import format_informe_bot
from scanner_volumen.config import load_config
from scanner_volumen.storage.db import open_readonly


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(
        prog="python -m scanner_volumen.bot",
        description="Informe del bot de ejecución. Solo lectura.",
    )
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--config", type=Path, default=Path("config.toml"))
    p.add_argument("--modo", default="paper")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    db_path = args.db if args.db is not None else Path(cfg.server.db_path)

    conn = open_readonly(db_path)
    try:
        repo = BotRepo(conn)
        print(format_informe_bot(
            repo, args.modo, repo.equity_inicial(defecto=cfg.bot.equity_inicial)
        ))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
