"""CLI: `python -m scanner_volumen.backtest.trajectory [--db PATH ...]`.

Abre la BD en solo-lectura, simula la estrategia de trayectoria y escribe el
informe a stdout. Los parámetros de la estrategia son flags para poder barrer
valores (p. ej. `--stop-pct`) sin tocar config.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scanner_volumen.backtest.db import open_readonly
from scanner_volumen.backtest.trajectory.loader import (
    load_transitions, make_candle_provider,
)
from scanner_volumen.backtest.trajectory.model import TrajectoryParams
from scanner_volumen.backtest.trajectory.portfolio import run_trajectory
from scanner_volumen.backtest.trajectory.report import format_trajectory_report
from scanner_volumen.config import load_config
from scanner_volumen.storage.repos import CandleRepo, StateTransitionRepo


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    d = TrajectoryParams()
    p = argparse.ArgumentParser(
        prog="python -m scanner_volumen.backtest.trajectory",
        description="Backtest de la estrategia de trayectoria sobre "
                    "state_transitions. Solo lectura.",
    )
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--config", type=Path, default=Path("config.toml"))
    p.add_argument("--equity", type=float, default=d.equity_inicial)
    p.add_argument("--margin-frac", type=float, default=d.fraccion_margen)
    p.add_argument("--leverage", type=float, default=d.apalancamiento)
    p.add_argument("--fee", type=float, default=d.comision_taker)
    p.add_argument("--stop-pct", type=float, default=d.stop_pct)
    p.add_argument("--max-concurrent", type=int, default=d.max_concurrentes)
    p.add_argument("--stale-min", type=int, default=d.stale_min)
    p.add_argument("--min-score", type=float, default=d.min_score_entrada)
    p.add_argument("--extreme-run-min", type=float, default=d.extreme_run_min)
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    db_path = args.db if args.db is not None else Path(cfg.server.db_path)

    params = TrajectoryParams(
        equity_inicial=args.equity, fraccion_margen=args.margin_frac,
        apalancamiento=args.leverage, comision_taker=args.fee,
        stop_pct=args.stop_pct, max_concurrentes=args.max_concurrent,
        stale_min=args.stale_min, min_score_entrada=args.min_score,
        extreme_run_min=args.extreme_run_min,
    )

    conn = open_readonly(db_path)
    try:
        transitions = load_transitions(StateTransitionRepo(conn))
        provider = make_candle_provider(CandleRepo(conn))
        run = run_trajectory(transitions, provider, params)
    finally:
        conn.close()

    print(format_trajectory_report(run))


if __name__ == "__main__":
    main()
