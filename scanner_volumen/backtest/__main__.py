"""Punto de entrada: `python -m scanner_volumen.backtest [--db PATH]`.

Abre la base en solo-lectura (nunca escribe, ver `storage/db.py`), calcula
todas las combinaciones de regla de entrada x horizonte de salida y escribe
el informe en texto plano a stdout.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scanner_volumen.backtest.report import format_report
from scanner_volumen.backtest.runner import run
from scanner_volumen.config import load_config
from scanner_volumen.storage.db import open_readonly
from scanner_volumen.storage.repos import SignalRepo


def main(argv: list[str] | None = None) -> None:
    # El informe usa acentos (texto en español, ver constraints del
    # proyecto). En Windows, `sys.stdout` sin reconfigurar hereda el
    # codepage de la consola (cp1252, o uno más limitado como cp437/850 en
    # consolas heredadas), que puede no representar esos caracteres y
    # lanzar `UnicodeEncodeError` a mitad de la tabla. Forzar UTF-8 con
    # `errors="replace"` evita ese crash: en el peor caso (consola sin
    # soporte UTF-8) se ven símbolos de reemplazo en vez de romper.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="python -m scanner_volumen.backtest",
        description="Mide expectativa por regla de entrada y horizonte de salida "
                     "sobre las señales ya grabadas por el scanner. Solo lectura.",
    )
    parser.add_argument(
        "--db", type=Path, default=None,
        help="ruta a la base SQLite (por defecto: server.db_path de config.toml)",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config.toml"),
        help="ruta al config.toml (por defecto: ./config.toml)",
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    db_path = args.db if args.db is not None else Path(cfg.server.db_path)

    conn = open_readonly(db_path)
    try:
        signal_repo = SignalRepo(conn)
        resultado = run(
            signal_repo,
            horizons=cfg.outcomes.horizons_minutes,
            gap_minutes=cfg.backtest.episode_gap_minutes,
            min_episodes_for_significance=cfg.backtest.min_episodes_for_significance,
            # fallback legado: solo parte el grupo centinela de señales sin
            # config_fingerprint real (ver backtest/segmentation.py).
            legacy_cutoff_ts=cfg.backtest.score_change_cutoff_ts,
        )
    finally:
        conn.close()

    print(format_report(resultado))


if __name__ == "__main__":
    main()
