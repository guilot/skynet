"""Corre el backtest sobre el historico reconstruido y aplica el criterio de
decision de `docs/criterio-de-decision.md`.

    .venv/bin/python herramientas/evaluar_criterio.py --db data/historico.db

El veredicto lo calcula ESTE script, escrito antes de ver ningun numero, no
una persona mirando una tabla y decidiendo despues que le parece suficiente.
Los tres puntos estan copiados literalmente del documento:

  1. Neto positivo EXCLUYENDO las 3 mejores operaciones.
  2. Neto positivo en al menos 2 de los 3 tercios temporales.
  3. Neto positivo despues de comisiones (ya incluidas en el calculo).

Si no se cumplen los tres, la estrategia NO ha demostrado ventaja. Ese
resultado no se negocia aqui: si se quiere cambiar el criterio, se cambia el
documento y se explica por que, no se ajusta el script hasta que pase.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from scanner_volumen.backtest.trajectory.loader import (
    load_transitions, make_candle_provider,
)
from scanner_volumen.backtest.trajectory.portfolio import run_trajectory
from scanner_volumen.storage.db import open_readonly
from scanner_volumen.storage.repos import CandleRepo, StateTransitionRepo
from scanner_volumen.strategy.model import StrategyParams


def _resumen(pnls: list[float]) -> str:
    if not pnls:
        return "sin trades"
    n = len(pnls)
    g = sum(1 for x in pnls if x > 0)
    return (f"{n:>4} trades  {100*g/n:>5.1f}% en verde  "
            f"neto {sum(pnls):>+9.2f}  medio {sum(pnls)/n:>+7.2f}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("data/historico.db"))
    p.add_argument("--config", type=Path, default=Path("config.toml"))
    args = p.parse_args()

    conn = open_readonly(args.db)
    trans = load_transitions(StateTransitionRepo(conn), None, None)
    run = run_trajectory(trans, make_candle_provider(CandleRepo(conn)),
                         StrategyParams())
    trades = sorted(run.trades, key=lambda t: t.outcome.entry_ts)
    pnls = [t.pnl for t in trades]
    conn.close()

    print(f"  transiciones reconstruidas: {len(trans):,}")
    print(f"  TOTAL   {_resumen(pnls)}\n")
    if not pnls:
        print("  sin trades: no hay nada que evaluar")
        raise SystemExit(2)

    fees = sum(t.fees for t in trades)
    print(f"  comisiones pagadas: {fees:.2f} USDT "
          f"({100*fees/(abs(sum(pnls))+fees):.0f}% del movimiento bruto)\n")

    # --- punto 1: sin las 3 mejores ---
    orden = sorted(pnls, reverse=True)
    sin3 = sum(orden[3:])
    p1 = sin3 > 0
    print(f"  [1] neto sin las 3 mejores ({sum(orden[:3]):+.2f}): {sin3:+.2f}"
          f"   -> {'CUMPLE' if p1 else 'NO CUMPLE'}")

    # --- punto 2: 2 de 3 tercios ---
    n = len(trades)
    tercios = [trades[:n//3], trades[n//3:2*n//3], trades[2*n//3:]]
    netos = [sum(t.pnl for t in g) for g in tercios]
    positivos = sum(1 for x in netos if x > 0)
    p2 = positivos >= 2
    print(f"  [2] tercios: " + "  ".join(f"{x:+.2f}" for x in netos)
          + f"   ({positivos}/3 positivos) -> {'CUMPLE' if p2 else 'NO CUMPLE'}")
    for i, g in enumerate(tercios, 1):
        print(f"        tercio {i}: {_resumen([t.pnl for t in g])}")

    # --- punto 3: neto tras comisiones (ya van descontadas) ---
    p3 = sum(pnls) > 0
    print(f"  [3] neto tras comisiones: {sum(pnls):+.2f}"
          f"   -> {'CUMPLE' if p3 else 'NO CUMPLE'}")

    print()
    if p1 and p2 and p3:
        print("  VEREDICTO: la estrategia CUMPLE los tres puntos del criterio.")
        print("  (Leerlo con los sesgos del documento delante: supervivencia del")
        print("   universo, market_cap aproximado, y la reconstruccion al ~76%.)")
        raise SystemExit(0)
    print("  VEREDICTO: la estrategia NO ha demostrado ventaja.")
    print("  Lo correcto es no operarla con dinero real, no buscar el parametro")
    print("  que la arregle: cada barrido adicional sobre esta misma muestra")
    print("  aumenta la probabilidad de encontrar un ganador por azar.")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
