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
    max_concurrentes: int = 0, arrancado_ms: int | None = None,
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
    # `arrancado_ms`, si se conoce, manda sobre el primer trade: un bot que
    # lleva catorce días corriendo y opera el primero no tiene una "Ventana"
    # de un día -y sin ventana real no hay con qué comparar al backtest-.
    ts_min = arrancado_ms if arrancado_ms is not None else min(ts, default=None)
    return ResumenOperativa(
        titulo=f"Bot en {modo}", trades=tuple(trades), descartes=etiquetas,
        equity_inicial=equity_inicial, equity_final=equity_final,
        ts_min=ts_min, ts_max=max(ts, default=None),
        total_transiciones=total_transiciones,
        max_concurrentes_alcanzado=max_concurrentes,
    )


def format_bloque_ejecucion(
    repo: BotRepo, modo: str, descartes: dict[str, int], cierres_tardios: int,
    saldo_real: float | None = None,
) -> str:
    cerradas = repo.cerradas(modo)
    abiertas = repo.abiertas(modo)
    entradas: list[float] = []
    salidas: dict[str, list[float]] = {}
    # fills sin `precio_regla` (motivos como EXTREME, que cierran a mercado
    # al vencer el temporizador): no hay nivel prometido contra el que medir,
    # así que se cuentan aparte y NUNCA se promedian con el resto -que nadie
    # pueda leer un "+0.0 bps" ahí como si fuera una medición real.
    sin_referencia: dict[str, int] = {}
    tardios_guardados = 0

    for fila in cerradas:
        direction = Direction(fila["direction"])
        d = _bps(fila["entry_price_senal"], fila["entry_price"], direction,
                 es_entrada=True)
        if d is not None:
            entradas.append(d)
        for f in repo.fills_de(fila["id"]):
            precio_regla = f["precio_regla"]
            if precio_regla is None:
                sin_referencia[f["reason"]] = sin_referencia.get(f["reason"], 0) + 1
            else:
                s = _bps(precio_regla, f["precio"], direction, es_entrada=False)
                if s is not None:
                    salidas.setdefault(f["reason"], []).append(s)
            tardios_guardados += int(f["tardio"])

    # Un fill tardío no espera a que su posición cierre para contar: una
    # salida parcial tardía en una posición que sigue abierta (`abiertas`) es
    # tan real como una en una ya cerrada. Si solo mirásemos `cerradas`, ese
    # cierre tardío quedaría invisible en el informe hasta que la posición
    # terminara de cerrarse.
    for fila in abiertas:
        for f in repo.fills_de(fila["id"]):
            tardios_guardados += int(f["tardio"])

    degradadas = sum(1 for fila in abiertas if fila.get("degradada"))

    lineas = ["== Ejecucion =="]
    lineas.append(
        f"Posiciones abiertas: {len(abiertas)} (degradadas: {degradadas})"
    )
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
    if sin_referencia:
        lineas.append(
            "Salidas a mercado sin nivel de referencia (no promediadas):"
        )
        for motivo, n in sin_referencia.items():
            lineas.append(f"  {motivo:<12} n={n}")
    lineas.append(f"Entradas descartadas por desvio: {descartes.get('desvio', 0)}")
    lineas.append(
        f"Cierres tardios por reinicio: {max(cierres_tardios, tardios_guardados)}"
    )

    # Bloque de modo real (Task 11): en `paper` no hay saldo real que
    # comparar ni exchange que reconcilie nada, así que el bloque NO
    # aparece -ni una línea distinta- para no romper el formato que los
    # tests de `paper` fijan como referencia.
    if modo != "paper":
        lineas.extend(_lineas_modo_real(repo, modo, saldo_real))
    return "\n".join(lineas)


def _lineas_modo_real(
    repo: BotRepo, modo: str, saldo_real: float | None,
) -> list[str]:
    """El bloque que solo se imprime cuando hay dinero real en juego (spec
    §9 y §10): el saldo real, el equity que calcula el bot y la diferencia
    entre ambos -la métrica que justifica toda la fase, porque mide todo lo
    que la estrategia no ve (funding, comisiones no modeladas, redondeos)-,
    más los cierres que decidió el exchange por su cuenta, las posiciones
    ajenas detectadas y los símbolos vetados, y cuántas veces se activó cada
    freno manual."""
    equity_bot = repo.equity(modo)
    contadores = repo.contadores(modo)
    lineas = ["", "Modo real:"]
    if saldo_real is None:
        # El proceso en vivo todavía no ha completado un tick en real (o
        # este informe corre contra una base anterior a que empezara a
        # persistirlo): mejor decirlo que fingir una diferencia de 0.00 que
        # no se ha medido.
        lineas.append(f"  Saldo real: sin dato todavia (equity calculado: {equity_bot:.2f})")
    else:
        diferencia = saldo_real - equity_bot
        lineas.append(f"  Saldo real: {saldo_real:.2f}")
        lineas.append(f"  Equity calculado: {equity_bot:.2f}")
        lineas.append(
            f"  Diferencia (funding, comisiones no modeladas, redondeos): "
            f"{diferencia:+.2f}"
        )
    # Dos caminos distintos detectan un cierre que decidió el exchange por su
    # cuenta -el stop saltó, o hubo liquidación- y ninguno marca al otro: el
    # sondeo en vivo (`BotRunner._cerrar_por_sondeo`, runner.py) lo ve
    # mientras el bot sigue corriendo; la reconciliación de arranque
    # (`_reconciliar_cerrada_en_exchange`) lo encuentra al arrancar, cuando
    # pasó con el bot caído. Se muestran POR SEPARADO en vez de sumados en un
    # único número: para quien opera no es lo mismo "pasó y lo vi al
    # momento" que "pasó y me enteré al reiniciar" -la segunda es la señal
    # de que hubo una ventana sin gobierno, que la primera no tiene.
    lineas.append(
        f"  Cierres ejecutados por el exchange (sondeo en vivo): "
        f"{contadores.get('cierres detectados por sondeo', 0)}"
    )
    lineas.append(
        f"  Cierres ejecutados por el exchange (detectados al arrancar): "
        f"{contadores.get('posiciones cerradas en el exchange', 0)}"
    )
    lineas.append(f"  Posiciones ajenas detectadas: {contadores.get('posiciones ajenas', 0)}")
    lineas.append(f"  Simbolos vetados: {contadores.get('simbolo vetado', 0)}")
    lineas.append(
        f"  Freno perdida diaria activado: {contadores.get('perdida diaria', 0)} veces"
    )
    lineas.append(
        f"  Freno parada de emergencia activado: "
        f"{contadores.get('parada de emergencia', 0)} veces"
    )
    return lineas


def format_informe_bot(
    repo: BotRepo, modo: str, equity_inicial: float,
    descartes: dict[str, int] | None = None, cierres_tardios: int = 0,
    total_transiciones: int = 0, max_concurrentes: int = 0,
    arrancado_ms: int | None = None, saldo_real: float | None = None,
) -> str:
    etiquetas = descartes or dict.fromkeys(ETIQUETAS_DESCARTE, 0)
    resumen = construir_resumen(repo, modo, equity_inicial, etiquetas,
                                total_transiciones, max_concurrentes, arrancado_ms)
    return (format_resumen(resumen) + "\n\n"
            + format_bloque_ejecucion(repo, modo, etiquetas, cierres_tardios, saldo_real))
