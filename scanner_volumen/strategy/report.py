"""Formatea el resultado de una operativa en texto plano.

Único formateador del proyecto: lo alimentan tanto el backtest (que construye
un `ResumenOperativa` desde su simulación) como el bot en vivo (que lo
construye desde su base de datos), para que los dos informes puedan compararse
línea a línea. Vive en `strategy/` y no en `backtest/` porque el bot no puede
depender del backtest.
"""
from __future__ import annotations

from datetime import datetime, timezone

from scanner_volumen.models import State
from scanner_volumen.strategy.model import ExitReason, ResumenOperativa

_SIGNAL_RANK = State.SIGNAL.rank

# Anchura a la que se rellena "descartes <etiqueta>:" en el bloque de
# descartes. No es cosmética: reproduce la alineación escrita a mano del
# informe original, que el golden master compara carácter a carácter.
_ANCHO_ETIQUETA_DESCARTE = 28


def _fecha(ms: int | None) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _drawdown_pct(resumen: ResumenOperativa) -> float:
    balance = resumen.equity_inicial
    pico = balance
    peor = 0.0
    for t in sorted(resumen.trades, key=lambda tr: tr.close_ts):
        balance += t.pnl
        pico = max(pico, balance)
        if pico > 0:
            peor = min(peor, (balance - pico) / pico)
    return peor * 100.0


def format_resumen(resumen: ResumenOperativa) -> str:
    lineas: list[str] = []
    lineas.append(f"== {resumen.titulo} ==")
    lineas.append(
        f"Ventana: {_fecha(resumen.ts_min)} -> {_fecha(resumen.ts_max)} (UTC) "
        f"— {resumen.total_transiciones} transiciones"
    )
    lineas.append("")
    lineas.append(f"Trades ejecutados: {len(resumen.trades)}")
    for etiqueta, cuenta in resumen.descartes.items():
        prefijo = f"descartes {etiqueta}:".ljust(_ANCHO_ETIQUETA_DESCARTE)
        lineas.append(f"  {prefijo}{cuenta}")
    lineas.append("")

    ret = (
        ((resumen.equity_final / resumen.equity_inicial) - 1) * 100
        if resumen.equity_inicial else 0.0
    )
    lineas.append(
        f"Equity: {resumen.equity_inicial:.2f} -> {resumen.equity_final:.2f} "
        f"({ret:+.2f}%)"
    )
    lineas.append(f"Drawdown maximo: {_drawdown_pct(resumen):.2f}%")
    lineas.append("")

    ganadores = [t for t in resumen.trades if t.pnl > 0]
    perdedores = [t for t in resumen.trades if t.pnl <= 0]
    n = len(resumen.trades)
    win_rate = (len(ganadores) / n * 100) if n else 0.0
    media_g = (sum(t.pnl for t in ganadores) / len(ganadores)) if ganadores else 0.0
    media_p = (sum(t.pnl for t in perdedores) / len(perdedores)) if perdedores else 0.0
    lineas.append(f"Win rate: {win_rate:.1f}%  ({len(ganadores)}/{n})")
    lineas.append(f"Media ganancia: {media_g:+.2f}   Media perdida: {media_p:+.2f}")

    # Cierres por fin de datos: la posición seguía abierta al final de la
    # ventana. No son salidas reales, así que se muestran aparte para no
    # contaminar las estadísticas anteriores. En el bot siempre es 0.
    fin_de_datos = [t for t in resumen.trades
                    if t.fills and t.fills[-1].reason == ExitReason.END_OF_DATA]
    pnl_fin_de_datos = sum(t.pnl for t in fin_de_datos)
    lineas.append(f"Cerrados por fin de datos (no son salidas reales): "
                  f"{len(fin_de_datos)} (PnL {pnl_fin_de_datos:+.2f})")
    lineas.append("")

    por_motivo: dict[str, float] = {r.value: 0.0 for r in ExitReason}
    for t in resumen.trades:
        for fill, pnl in zip(t.fills, t.fill_pnls):
            por_motivo[fill.reason.value] += pnl
    lineas.append("PnL por motivo de salida:")
    for motivo, pnl in por_motivo.items():
        lineas.append(f"  {motivo:<12} {pnl:+.2f}")
    lineas.append("")

    # Resultado central de la tesis: ¿compensa el edge de los pocos símbolos que
    # escalan (runners) el arrastre de los que revierten sin llegar a SIGNAL?
    runners = [t for t in resumen.trades if t.max_rank >= _SIGNAL_RANK]
    arrastre = [t for t in resumen.trades if t.max_rank < _SIGNAL_RANK]
    lineas.append(f"Runners (alcanzan SIGNAL+): {len(runners)} trades, "
                  f"PnL {sum(t.pnl for t in runners):+.2f}")
    lineas.append(f"Arrastre (no pasan de HOT): {len(arrastre)} trades, "
                  f"PnL {sum(t.pnl for t in arrastre):+.2f}")
    lineas.append(f"Concurrencia máxima: {resumen.max_concurrentes_alcanzado}")

    return "\n".join(lineas)
