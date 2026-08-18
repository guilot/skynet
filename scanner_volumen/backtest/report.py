"""Formato de texto plano del informe de backtest.

Requisito 3: el umbral de episodios decide si una fila lleva la marca
"MUESTRA INSUFICIENTE"; esta herramienta MIDE, nunca recomienda -no hay
ninguna función aquí que elija "la mejor" fila. Requisito 4: se documenta
aquí, en el propio informe, por qué no se simula stop/target.
"""
from __future__ import annotations

from datetime import datetime, timezone

from scanner_volumen.backtest.runner import BacktestRun, ComboResult

_ANCHO_REGLA = 32


def _pct(valor: float | None) -> str:
    return "n/a" if valor is None else f"{valor:+.2f}%"


def _win(valor: float | None) -> str:
    return "n/a" if valor is None else f"{valor * 100:.0f}%"


def _fmt_ts(ts: int | None) -> str:
    if ts is None:
        return "n/a"
    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _cabecera(run: BacktestRun) -> list[str]:
    return [
        "=" * 100,
        " BACKTEST DE SEÑALES — scanner_volumen",
        "=" * 100,
        f" Ventana de señales : {_fmt_ts(run.ts_min)} .. {_fmt_ts(run.ts_max)}",
        f" Total              : {run.total_signals} señales, {run.total_episodes} episodios"
        f" (hueco de episodio: {run.gap_minutes:g} min)",
        "",
    ]


def _aviso_metodologico(run: BacktestRun) -> list[str]:
    return [
        "AVISO METODOLÓGICO",
        "-" * 100,
        "- Ajuste por dirección: toda cifra de P&L y de excursión favorable/",
        "  adversa está calculada sobre la posición, no sobre el precio crudo.",
        "  Para una señal SHORT el retorno se invierte, y la excursión",
        "  favorable/adversa se deriva de mae_pct/mfe_pct respectivamente (no",
        "  al revés): lo favorable para un short es que el precio caiga.",
        "- El EPISODIO (no la señal) es la unidad de muestra: señales",
        "  consecutivas del mismo símbolo separadas por menos de "
        f"{run.gap_minutes:g} min",
        "  se agrupan en un único episodio antes de promediar, para que una",
        "  racha larga en un solo símbolo no domine el resultado. La columna",
        "  N_EP (episodios) es la cifra titular; N_SEÑ se muestra solo como",
        "  referencia.",
        "- Combinaciones con menos de "
        f"{run.min_episodes_for_significance} episodios están marcadas",
        "  [MUESTRA INSUFICIENTE]: no son estadísticamente significativas y no",
        "  deben tratarse como una conclusión. Esta herramienta mide; no",
        "  recomienda ninguna regla ni horizonte -esa decisión es humana-.",
        "- Sobre stop/target: este informe NO simula una salida combinada del",
        "  tipo \"stop -1% / target +2%\". MFE y MAE registran los extremos",
        "  alcanzados dentro del horizonte pero no el ORDEN en que se",
        "  tocaron: si ambos se tocaron, no hay forma de saber cuál ocurrió",
        "  primero a partir de estos datos. Simular esa regla exigiría asumir",
        "  en silencio que el stop se dispara primero (una cota inferior, no",
        "  un resultado real), así que se omite en vez de presentar una",
        "  suposición como un hecho medido.",
        "",
    ]


def _segmentacion(run: BacktestRun) -> list[str]:
    s = run.segmentation
    return [
        "SEGMENTACIÓN POR CAMBIO DE SCORING",
        "-" * 100,
        f" Corte: ts < {s.cutoff_ts} ({_fmt_ts(s.cutoff_ts)}) tenía una",
        "  penalización de extensión sobre VWAP más débil; no es estrictamente",
        "  comparable con lo posterior. Los resultados de la tabla combinan",
        "  ambos periodos.",
        f" Antes del corte : {s.n_before} señales, {s.episodes_before} episodios",
        f" Desde el corte  : {s.n_after} señales, {s.episodes_after} episodios",
        "",
    ]


_COLS = (
    ("REGLA", _ANCHO_REGLA, "l"),
    ("HZ_MIN", 6, "r"),
    ("N_SEÑ", 6, "r"),
    ("N_EP", 5, "r"),
    ("PNL%_EP_MEDIA", 14, "r"),
    ("PNL%_EP_MED", 12, "r"),
    ("WIN%_EP", 8, "r"),
    ("FAV%_EP", 9, "r"),
    ("ADV%_EP", 9, "r"),
    ("PNL%_SEÑ_MEDIA", 15, "r"),
    ("AVISO", 22, "l"),
)


def _fila(valores: list[str]) -> str:
    partes = []
    for (_, ancho, alineacion), valor in zip(_COLS, valores):
        partes.append(valor.ljust(ancho) if alineacion == "l" else valor.rjust(ancho))
    return " ".join(partes)


def _tabla(run: BacktestRun) -> list[str]:
    lineas = [
        "COMPARACIÓN POR REGLA DE ENTRADA x HORIZONTE DE SALIDA",
        "-" * 100,
        _fila([nombre for nombre, _, _ in _COLS]),
        _fila(["-" * ancho for _, ancho, _ in _COLS]),
    ]
    for combo in run.results:
        lineas.append(_fila_de_combo(combo, run.min_episodes_for_significance))
    if not run.results:
        lineas.append("(sin combinaciones que mostrar)")
    return lineas


def _fila_de_combo(combo: ComboResult, min_episodios: int) -> str:
    ep = combo.per_episode
    señ = combo.per_signal
    aviso = "[MUESTRA INSUFICIENTE]" if ep.n < min_episodios else ""
    return _fila([
        combo.rule.label,
        str(combo.horizon),
        str(señ.n),
        str(ep.n),
        _pct(ep.mean_pnl),
        _pct(ep.median_pnl),
        _win(ep.win_rate),
        _pct(ep.mean_favourable),
        _pct(ep.mean_adverse),
        _pct(señ.mean_pnl),
        aviso,
    ])


def format_report(run: BacktestRun) -> str:
    """Construye el informe completo en texto plano. Nunca elige ni sugiere
    una fila "mejor": el humano decide, esta función solo presenta lo
    medido."""
    lineas: list[str] = []
    lineas.extend(_cabecera(run))
    lineas.extend(_aviso_metodologico(run))
    lineas.extend(_segmentacion(run))
    lineas.extend(_tabla(run))
    return "\n".join(lineas)
