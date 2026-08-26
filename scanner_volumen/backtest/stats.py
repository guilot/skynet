"""Ajuste por dirección (requisito 1) y agregación por señal / por episodio
(requisito 2), con todo cociente comprobando su denominador (requisito de
constraints globales)."""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from scanner_volumen.backtest.episodes import Episode


def adjusted_return(direction: str, return_pct: float) -> float:
    """P&L direccional. `return_pct` es el cambio de precio crudo; una
    posición SHORT gana cuando el precio cae, así que se invierte el signo.
    NEUTRAL se trata como LONG (no se invierte): no representa una posición
    con dirección propia, así que invertirlo no tendría sentido y dejarlo
    tal cual mantiene la agregación honesta si alguna vez aparece."""
    return -return_pct if direction == "SHORT" else return_pct


def adjusted_favourable(direction: str, mfe_pct: float, mae_pct: float) -> float:
    """Excursión favorable direccional. Para un LONG es `mfe_pct` (el máximo
    alcanzado) tal cual. Para un SHORT lo favorable es que el precio caiga,
    así que se deriva de `mae_pct` (el mínimo alcanzado) invertido -nunca de
    `mfe_pct`, que para un short mide justo lo contrario-."""
    return -mae_pct if direction == "SHORT" else mfe_pct


def adjusted_adverse(direction: str, mfe_pct: float, mae_pct: float) -> float:
    """Excursión adversa direccional: espejo de `adjusted_favourable`. Para
    un SHORT lo adverso es que el precio suba, así que se deriva de
    `mfe_pct` invertido."""
    return -mfe_pct if direction == "SHORT" else mae_pct


@dataclass(frozen=True)
class HorizonStats:
    """Resumen de una combinación (regla de entrada, horizonte), ya sea
    calculado sobre señales individuales o sobre episodios. Todos los campos
    salvo `n` son `None` cuando `n == 0`: ningún cociente se calcula sobre un
    denominador vacío.

    `mixed_direction_episodes` (hallazgo 3) solo tiene sentido en la versión
    por-episodio: cuenta cuántos de los episodios que aportan valor mezclan
    más de una dirección entre sus miembros calificados (p. ej. un LONG y un
    SHORT casi simultáneos del mismo símbolo bajo una regla /ALL). En la
    versión por-señal se queda en su valor por defecto (0): ahí no existe el
    concepto de episodio."""

    n: int
    mean_pnl: float | None
    median_pnl: float | None
    win_rate: float | None
    mean_favourable: float | None
    mean_adverse: float | None
    mixed_direction_episodes: int = 0


def _resumen(
    valores: list[tuple[float, float, float]], mixed_direction_episodes: int = 0
) -> HorizonStats:
    n = len(valores)
    if n == 0:
        return HorizonStats(
            n=0, mean_pnl=None, median_pnl=None, win_rate=None,
            mean_favourable=None, mean_adverse=None, mixed_direction_episodes=0,
        )
    pnls = [v[0] for v in valores]
    favs = [v[1] for v in valores]
    advs = [v[2] for v in valores]
    ganadoras = sum(1 for p in pnls if p > 0)
    return HorizonStats(
        n=n,
        mean_pnl=statistics.mean(pnls),
        median_pnl=statistics.median(pnls),
        win_rate=ganadoras / n,
        mean_favourable=statistics.mean(favs),
        mean_adverse=statistics.mean(advs),
        mixed_direction_episodes=mixed_direction_episodes,
    )


def compute_combo_stats(
    qualifying_signals: list[dict],
    all_episodes: list[Episode],
    outcomes_by_signal: dict[int, dict[int, dict]],
    horizon: int,
    fade: bool = False,
) -> tuple[HorizonStats, HorizonStats]:
    """Calcula las estadísticas por-señal y por-episodio para una
    combinación (regla de entrada ya aplicada -> `qualifying_signals`,
    horizonte de salida). `outcomes_by_signal` mapea signal_id -> horizonte
    -> fila de `signal_outcomes` (con return_pct/mfe_pct/mae_pct).

    `all_episodes` (hallazgo 1) viene YA agrupado sobre TODAS las señales del
    periodo, no solo sobre `qualifying_signals`. Agrupar sobre el subconjunto
    filtrado por la regla estaba mal: una racha física continua de un
    símbolo (huecos reales por debajo de `episode_gap_minutes`) con señales
    intermedias que la regla excluye (p. ej. un tramo SHORT en medio de un
    tramo LONG bajo una regla */LONG) podía dejar a las señales
    supervivientes separadas por más del hueco configurado y partirse en
    varios "episodios independientes" que en realidad son la misma racha
    -justo la correlación que el episodio existe para eliminar-. Agrupando
    sobre la población completa una sola vez (ver `runner.compute_all`) y
    después intersecando cada episodio con las señales que sí califican, el
    recuento de episodios deja de depender de qué regla se esté evaluando.

    Por-episodio (requisito 2, la cifra titular): cada episodio aporta UN
    valor si al menos una de sus señales califica para la regla Y tiene
    resultado en este horizonte -la media de esas señales miembro
    calificadas-, así que una racha de 36 señales pesa lo mismo que una
    señal aislada. Un episodio sin ningún miembro calificado con resultado
    en este horizonte se descarta para este horizonte (no aporta un cero
    falso).

    `fade`: si es `True`, cada punto (pnl, fav, adv) ya ajustado por
    dirección se invierte -fadear una señal es tomar la posición contraria-.
    El P&L se niega tal cual; lo favorable y lo adverso se intercambian Y SE
    NIEGAN (no basta con el intercambio): lo que era adverso para la señal
    original, negado, pasa a ser favorable para su fade, y viceversa. Negar
    es obligatorio porque el fade es la posición contraria -si el precio
    subió hasta mfe_pct=+3.0 (adverso para un short que fadea un long) eso
    se traduce en una adversa de fade de -3.0, no de +3.0-; sin la negación,
    la favorable del fade podía salir negativa y la adversa positiva, lo
    cual es semánticamente imposible (favorable es el mejor punto a favor,
    siempre >= 0 si hubo movimiento a favor; adversa el peor punto en
    contra, siempre <= 0 si hubo movimiento en contra).
    `qualifying_signals` ya viene filtrado por `EntryRule.matches`
    sobre la señal ORIGINAL -el fade no cambia qué señales entran aquí, solo
    cómo se puntúa cada una-. Una señal NEUTRAL no se trata distinto aquí:
    `adjusted_return`/`adjusted_favourable`/`adjusted_adverse` ya la tratan
    como un LONG (no invierten), así que su fade es, sin más, la negación de
    ese mismo valor -no hay una rama NEUTRAL separada que mantener."""
    puntos: dict[int, tuple[float, float, float]] = {}
    direcciones: dict[int, str] = {}
    for s in qualifying_signals:
        direcciones[s["id"]] = s["direction"]
        outcome = outcomes_by_signal.get(s["id"], {}).get(horizon)
        if outcome is None:
            continue
        direction = s["direction"]
        pnl = adjusted_return(direction, outcome["return_pct"])
        fav = adjusted_favourable(direction, outcome["mfe_pct"], outcome["mae_pct"])
        adv = adjusted_adverse(direction, outcome["mfe_pct"], outcome["mae_pct"])
        if fade:
            pnl, fav, adv = -pnl, -adv, -fav
        puntos[s["id"]] = (pnl, fav, adv)

    stats_señal = _resumen(list(puntos.values()))

    valores_episodio: list[tuple[float, float, float]] = []
    episodios_mixtos = 0
    for ep in all_episodes:
        miembros_ids = [sid for sid in ep.signal_ids if sid in puntos]
        if not miembros_ids:
            continue
        miembros = [puntos[sid] for sid in miembros_ids]
        pnl_medio = statistics.mean(m[0] for m in miembros)
        fav_medio = statistics.mean(m[1] for m in miembros)
        adv_medio = statistics.mean(m[2] for m in miembros)
        valores_episodio.append((pnl_medio, fav_medio, adv_medio))
        if len({direcciones[sid] for sid in miembros_ids}) > 1:
            episodios_mixtos += 1
    stats_episodio = _resumen(valores_episodio, mixed_direction_episodes=episodios_mixtos)

    return stats_señal, stats_episodio
