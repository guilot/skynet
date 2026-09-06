"""Informe del bot: el mismo formato que el backtest, más la ejecución.

La parte de estrategia se delega en el formateador compartido, para que los dos
informes se puedan poner lado a lado. Lo que el backtest no puede decirte -y es
el motivo de la Fase 2- va en un bloque aparte: cuánto se aleja el precio que
consigues del que la regla pedía.
"""
from __future__ import annotations

from scanner_volumen.bot.model import ETIQUETAS_DESCARTE
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.models import Direction
from scanner_volumen.strategy.model import (
    ExitReason, Fill, ResumenOperativa, TradeResumen,
)
from scanner_volumen.strategy.report import format_resumen

BPS = 10_000.0


def _bps(referencia: float, obtenido: float, direction: Direction,
         es_entrada: bool) -> float | None:
    """Coste de ejecución en puntos básicos. **Positivo = peor para nosotros.**

    Al entrar, un LONG sufre si paga por encima de la señal; un SHORT, si vende
    por debajo. Al salir es al revés. Unificar el signo permite promediar
    entradas y salidas de las dos direcciones sin que se cancelen entre sí."""
    if referencia <= 0:
        return None
    if es_entrada:
        peor = (obtenido - referencia) if direction is Direction.LONG else (referencia - obtenido)
    else:
        peor = (referencia - obtenido) if direction is Direction.LONG else (obtenido - referencia)
    return peor / referencia * BPS


def construir_resumen(
    repo: BotRepo, modo: str, equity_inicial: float,
    descartes: dict[str, int] | None = None, total_transiciones: int = 0,
    max_concurrentes: int = 0,
) -> ResumenOperativa:
    cerradas = repo.cerradas(modo)
    trades: list[TradeResumen] = []
    for fila in cerradas:
        filas_fill = repo.fills_de(fila["id"])
        fills = tuple(
            Fill(ts=f["ts"], price=f["precio"], fraction=f["fraction"],
                 reason=ExitReason(f["reason"]))
            for f in filas_fill
        )
        direction = Direction(fila["direction"])
        signo = 1.0 if direction is Direction.LONG else -1.0
        fill_pnls = tuple(
            signo * (f["precio"] - fila["entry_price"]) * fila["size"] * f["fraction"]
            - f["comision"]
            for f in filas_fill
        )
        trades.append(TradeResumen(
            symbol=fila["symbol"], direction=direction, entry_ts=fila["entry_ts"],
            entry_price=fila["entry_price"], close_ts=fila["close_ts"],
            fills=fills, fill_pnls=fill_pnls, margin=fila["margin"],
            pnl=fila["pnl"], fees=fila["fees"], max_rank=fila["max_rank"] or 0,
        ))

    ts = [t.entry_ts for t in trades] + [t.close_ts for t in trades]
    equity_final = equity_inicial + sum(t.pnl for t in trades)
    etiquetas = descartes or dict.fromkeys(ETIQUETAS_DESCARTE, 0)
    return ResumenOperativa(
        titulo=f"Bot en {modo}", trades=tuple(trades), descartes=etiquetas,
        equity_inicial=equity_inicial, equity_final=equity_final,
        ts_min=min(ts, default=None), ts_max=max(ts, default=None),
        total_transiciones=total_transiciones,
        max_concurrentes_alcanzado=max_concurrentes,
    )


def format_bloque_ejecucion(
    repo: BotRepo, modo: str, descartes: dict[str, int], cierres_tardios: int,
) -> str:
    cerradas = repo.cerradas(modo)
    entradas: list[float] = []
    salidas: dict[str, list[float]] = {}
    tardios_guardados = 0

    for fila in cerradas:
        direction = Direction(fila["direction"])
        d = _bps(fila["entry_price_senal"], fila["entry_price"], direction,
                 es_entrada=True)
        if d is not None:
            entradas.append(d)
        for f in repo.fills_de(fila["id"]):
            s = _bps(f["precio_referencia"], f["precio"], direction,
                     es_entrada=False)
            if s is not None:
                salidas.setdefault(f["reason"], []).append(s)
            tardios_guardados += int(f["tardio"])

    # Un fill tardío no espera a que su posición cierre para contar: una
    # salida parcial tardía en una posición que sigue abierta (`abiertas`) es
    # tan real como una en una ya cerrada. Si solo mirásemos `cerradas`, ese
    # cierre tardío quedaría invisible en el informe hasta que la posición
    # terminara de cerrarse.
    for fila in repo.abiertas(modo):
        for f in repo.fills_de(fila["id"]):
            tardios_guardados += int(f["tardio"])

    lineas = ["== Ejecucion =="]
    if entradas:
        lineas.append(
            f"Desvio de entrada: medio {sum(entradas)/len(entradas):+.1f} bps   "
            f"peor {max(entradas):+.1f} bps   (n={len(entradas)})"
        )
    else:
        lineas.append("Desvio de entrada: sin datos (n=0)")
    lineas.append("Desvio de salida por motivo:")
    if salidas:
        for motivo, valores in salidas.items():
            lineas.append(
                f"  {motivo:<12} {sum(valores)/len(valores):+.1f} bps  "
                f"(n={len(valores)})"
            )
    else:
        lineas.append("  sin datos (n=0)")
    lineas.append(f"Entradas descartadas por desvio: {descartes.get('desvio', 0)}")
    lineas.append(
        f"Cierres tardios por reinicio: {max(cierres_tardios, tardios_guardados)}"
    )
    return "\n".join(lineas)


def format_informe_bot(
    repo: BotRepo, modo: str, equity_inicial: float,
    descartes: dict[str, int] | None = None, cierres_tardios: int = 0,
    total_transiciones: int = 0, max_concurrentes: int = 0,
) -> str:
    etiquetas = descartes or dict.fromkeys(ETIQUETAS_DESCARTE, 0)
    resumen = construir_resumen(repo, modo, equity_inicial, etiquetas,
                                total_transiciones, max_concurrentes)
    return (format_resumen(resumen) + "\n\n"
            + format_bloque_ejecucion(repo, modo, etiquetas, cierres_tardios))
