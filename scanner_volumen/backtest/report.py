"""Formato de texto plano del informe de backtest.

Requisito 3: el umbral de episodios decide si una fila lleva la marca
"MUESTRA INSUFICIENTE"; esta herramienta MIDE, nunca recomienda -no hay
ninguna función aquí que elija "la mejor" fila. Requisito 4: se documenta
aquí, en el propio informe, por qué no se simula stop/target.
"""
from __future__ import annotations

from datetime import datetime, timezone

from scanner_volumen.backtest.runner import BacktestRun, ComboResult
from scanner_volumen.provenance import PRE_PROVENANCE_SENTINEL

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
        # Hallazgo 4: candles_seen/candles_expected existen justo para poder
        # distinguir un resultado calculado sobre una ventana completa de
        # uno calculado sobre un hueco de datos. No se excluyen de ningún
        # cálculo -esta herramienta mide, no depura-, pero el conteo se
        # imprime aquí, en la cabecera, para que sea imposible de pasar por
        # alto si algún día deja de ser cero.
        f" Resultados incompletos: {run.incomplete_outcomes} fila(s) de"
        " signal_outcomes con candles_seen < candles_expected (ventana con"
        " hueco de datos; no se excluyen de los cálculos de abajo)",
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
        "  referencia. El agrupado se hace UNA vez sobre todas las señales",
        "  del periodo, no por regla: una racha física continua con señales",
        "  intermedias que una regla concreta excluye (p. ej. un tramo SHORT",
        "  en medio de un tramo LONG bajo una regla */LONG) sigue siendo un",
        "  único episodio, no se parte en varios solo porque los IDs que",
        "  sobreviven al filtro queden más separados entre sí que el hueco",
        "  configurado.",
        "- En las reglas /ALL, un episodio puede mezclar señales LONG y",
        "  SHORT casi simultáneas del mismo símbolo (p. ej. un giro de",
        "  tendencia). Su valor de episodio es la media aritmética de esas",
        "  señales YA ajustadas por dirección, que por construcción tienden",
        "  a cancelarse entre sí -no se excluyen, se promedian tal cual-.",
        "  La columna MIXTOS_EP cuenta cuántos episodios de cada fila caen",
        "  en este caso; por diseño siempre es 0 en las reglas /LONG y",
        "  /SHORT, que nunca mezclan dirección.",
        "- WIN%_EP es la fracción de EPISODIOS cuya media de P&L salió",
        "  positiva, no la fracción de señales u operaciones ganadoras: un",
        "  episodio de 10 señales con nueve pequeñas pérdidas y una gran",
        "  ganancia cuenta como \"ganador\" si la media sale positiva.",
        "- Combinaciones con menos de "
        f"{run.min_episodes_for_significance} episodios están marcadas",
        "  [MUESTRA INSUFICIENTE]: no son estadísticamente significativas y no",
        "  deben tratarse como una conclusión. Esta herramienta mide; no",
        "  recomienda ninguna regla ni horizonte -esa decisión es humana-.",
        "- Sobre stop/target: este informe NO simula una salida combinada del",
        "  tipo \"stop -1% / target +2%\" (ni para las reglas normales ni para",
        "  las FADE). MFE y MAE registran los extremos alcanzados dentro del",
        "  horizonte pero no el ORDEN en que se tocaron: si ambos se tocaron,",
        "  no hay forma de saber cuál ocurrió primero (SL/TP) a partir de",
        "  estos datos. Simular esa regla exigiría asumir en silencio que el",
        "  stop se dispara primero (una cota inferior, no un resultado real),",
        "  así que se omite en vez de presentar una suposición como un hecho",
        "  medido.",
        "- Sobre las reglas FADE: el análisis sobre 243 episodios mostró que",
        "  las señales del scanner tienen edge negativo en su propia",
        "  dirección -entran en el agotamiento del momentum, y el precio",
        "  revierte-. FADEAR una señal es tomar la posición contraria: su",
        "  P&L es la negación exacta del P&L direccional de la señal, y su",
        "  favorable/adversa son la adversa/favorable de la señal",
        "  intercambiadas. El filtro de estado y dirección de la regla se",
        "  aplica siempre sobre la señal ORIGINAL -decide QUÉ señales se",
        "  toman-; el fade solo cambia CÓMO se opera cada una.",
        "  IMPORTANTE -qué NO es una fila FADE, aunque su P&L salga positivo:",
        "  * Mide un edge DIRECCIONAL a horizonte fijo: no es una estrategia operable,",
        "    no incluye gestión de entrada/salida más allá de ese horizonte.",
        "  * No modela stop-loss ni take-profit (SL/TP): igual que en la",
        "    nota anterior, MFE/MAE no registran el orden de toque, así que",
        "    no se puede derivar un resultado con stop/target de estos datos",
        "    -tampoco para el fade-.",
        "  * No modela apalancamiento, liquidación, slippage ni funding. La",
        "    columna ADV%_EP es el riesgo real: con apalancamiento, esa",
        "    excursión adversa es lo que se convierte en liquidación.",
        "  * Está medida sobre un ÚNICO régimen de mercado -cuatro días de un",
        "    mercado en reversión-. El fade es, en esencia, una apuesta a la",
        "    reversión a la media; un régimen de tendencia sostenida podría",
        "    invertir el signo del resultado, y ningún periodo de tendencia",
        "    forma parte de esta muestra.",
        "",
    ]


def _segmentacion(run: BacktestRun) -> list[str]:
    lineas = [
        "SEGMENTACIÓN POR PROCEDENCIA (config_fingerprint)",
        "-" * 100,
        " Cada grupo comparte el mismo fingerprint de configuración -curvas",
        "  de score, umbrales de estado, parámetros de motor/universo/perfil-",
        "  que produjo esas señales; grupos distintos no son estrictamente",
        "  comparables entre sí. Los resultados de la tabla de abajo combinan",
        "  todos los grupos.",
        f" Filas grabadas antes de que existiera esta columna comparten el",
        f"  centinela {PRE_PROVENANCE_SENTINEL!r} -no se pueden distinguir por",
        "  fingerprint entre sí-; si config.toml define un corte legado",
        "  (backtest.score_change_cutoff_ts), ese grupo se parte además en un",
        "  antes/después de ese corte, la única frontera de scoring conocida",
        "  dentro de esas filas.",
        "",
    ]
    if not run.segmentation:
        lineas.append(" (sin señales)")
        lineas.append("")
        return lineas
    for g in run.segmentation:
        lineas.append(
            f" {g.label}: {g.n_signals} señales, {g.n_episodes} episodios "
            f"({_fmt_ts(g.ts_min)} .. {_fmt_ts(g.ts_max)})"
        )
    if len(run.segmentation) > 1:
        lineas.append(
            " Nota: la suma de episodios de todos los grupos puede superar el"
        )
        lineas.append(
            "  total de episodios de la cabecera -un mismo episodio físico"
        )
        lineas.append(
            "  que cruza el límite entre dos grupos se cuenta una vez en cada"
        )
        lineas.append(
            "  lado, porque cada grupo se agrupa por separado (ver"
        )
        lineas.append("  `segmentation.compute_segmentation`).")
    lineas.append("")
    return lineas


_COLS = (
    ("REGLA", _ANCHO_REGLA, "l"),
    ("HZ_MIN", 6, "r"),
    ("N_SEÑ", 6, "r"),
    ("N_EP", 5, "r"),
    ("MIXTOS_EP", 9, "r"),
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
        str(ep.mixed_direction_episodes),
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
