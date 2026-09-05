"""Golden master del backtest de trayectoria.

Congela la salida del backtest sobre la base de datos real para que el
refactor a `strategy/` pueda demostrar que no cambia ningún resultado. Vuelca
el detalle de cada trade, no solo el resumen: dos cambios que se compensen
darían el mismo equity final y pasarían desapercibidos.

La base de datos (~200 MB) no está versionada. El test se salta si no está.
Para regenerar el fichero congelado tras un cambio INTENCIONADO de
comportamiento: `SCANNER_BT_REGENERA=1 .venv/bin/pytest \
tests/backtest/trajectory/test_golden_master.py`
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from scanner_volumen.backtest.db import open_readonly
from scanner_volumen.backtest.trajectory.loader import (
    load_transitions, make_candle_provider,
)
from scanner_volumen.backtest.trajectory.model import TrajectoryParams
from scanner_volumen.backtest.trajectory.portfolio import run_trajectory
from scanner_volumen.backtest.trajectory.report import format_trajectory_report
from scanner_volumen.storage.repos import CandleRepo, StateTransitionRepo

RAIZ = Path(__file__).resolve().parents[3]
DB = Path(os.environ.get("SCANNER_BT_DB", RAIZ / ".backtest-data" / "scanner.db"))
CONGELADO = Path(__file__).parent / "golden_trajectory.txt"


def _volcado(run) -> str:
    """Informe + detalle de cada fill. Los formatos `.10g` / `.10f` fijan
    suficientes decimales para que un cambio real de aritmética salte, sin
    exponer el ruido del último bit de coma flotante."""
    lineas = [format_trajectory_report(run), "", "== Detalle de trades =="]
    for t in sorted(run.trades, key=lambda tr: (tr.outcome.close_ts, tr.outcome.symbol)):
        o = t.outcome
        lineas.append(
            f"{o.symbol} {o.direction.value} entry_ts={o.entry_ts} "
            f"entry_price={o.entry_price:.10g} max_rank={o.max_rank} "
            f"margin={t.margin:.10f} pnl={t.pnl:.10f} fees={t.fees:.10f}"
        )
        for f, pnl in zip(o.fills, t.fill_pnls):
            lineas.append(
                f"    fill ts={f.ts} price={f.price:.10g} "
                f"fraction={f.fraction:.10f} reason={f.reason.value} "
                f"pnl={pnl:.10f}"
            )
    return "\n".join(lineas)


def _correr() -> str:
    conn = open_readonly(DB)
    try:
        transitions = load_transitions(StateTransitionRepo(conn))
        provider = make_candle_provider(CandleRepo(conn))
        run = run_trajectory(transitions, provider, TrajectoryParams())
    finally:
        conn.close()
    return _volcado(run)


@pytest.mark.skipif(
    not DB.exists(),
    reason=f"base de datos del backtest no encontrada en {DB}; "
           "el golden master no puede comprobarse (ver SCANNER_BT_DB)",
)
def test_golden_master_del_backtest():
    actual = _correr()
    if os.environ.get("SCANNER_BT_REGENERA"):
        CONGELADO.write_text(actual, encoding="utf-8")
        pytest.skip("golden master regenerado")
    assert actual == CONGELADO.read_text(encoding="utf-8")
