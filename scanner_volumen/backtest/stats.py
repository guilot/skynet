"""Ajuste por dirección (requisito 1) y agregación por señal / por episodio
(requisito 2), con todo cociente comprobando su denominador (requisito de
constraints globales)."""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from scanner_volumen.backtest.episodes import group_episodes


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
    denominador vacío."""

    n: int
    mean_pnl: float | None
    median_pnl: float | None
    win_rate: float | None
    mean_favourable: float | None
    mean_adverse: float | None


def _resumen(valores: list[tuple[float, float, float]]) -> HorizonStats:
    n = len(valores)
    if n == 0:
        return HorizonStats(
            n=0, mean_pnl=None, median_pnl=None, win_rate=None,
            mean_favourable=None, mean_adverse=None,
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
    )


def compute_combo_stats(
    qualifying_signals: list[dict],
    outcomes_by_signal: dict[int, dict[int, dict]],
    horizon: int,
    gap_minutes: float,
) -> tuple[HorizonStats, HorizonStats]:
    """Calcula las estadísticas por-señal y por-episodio para una
    combinación (regla de entrada ya aplicada -> `qualifying_signals`,
    horizonte de salida). `outcomes_by_signal` mapea signal_id -> horizonte
    -> fila de `signal_outcomes` (con return_pct/mfe_pct/mae_pct).

    Por-episodio (requisito 2, la cifra titular): se agrupan las señales
    calificadas en episodios (independiente del horizonte -el hueco entre
    señales no depende de qué resultado exista-); cada episodio aporta UN
    valor, la media de sus señales miembro que sí tienen resultado en este
    horizonte, así que una racha de 36 señales pesa lo mismo que una señal
    aislada. Un episodio sin ningún miembro con resultado en este horizonte
    se descarta para este horizonte (no aporta un cero falso).
    """
    puntos: dict[int, tuple[float, float, float]] = {}
    for s in qualifying_signals:
        outcome = outcomes_by_signal.get(s["id"], {}).get(horizon)
        if outcome is None:
            continue
        direction = s["direction"]
        pnl = adjusted_return(direction, outcome["return_pct"])
        fav = adjusted_favourable(direction, outcome["mfe_pct"], outcome["mae_pct"])
        adv = adjusted_adverse(direction, outcome["mfe_pct"], outcome["mae_pct"])
        puntos[s["id"]] = (pnl, fav, adv)

    stats_señal = _resumen(list(puntos.values()))

    episodios = group_episodes(qualifying_signals, gap_minutes)
    valores_episodio: list[tuple[float, float, float]] = []
    for ep in episodios:
        miembros = [puntos[sid] for sid in ep.signal_ids if sid in puntos]
        if not miembros:
            continue
        pnl_medio = statistics.mean(m[0] for m in miembros)
        fav_medio = statistics.mean(m[1] for m in miembros)
        adv_medio = statistics.mean(m[2] for m in miembros)
        valores_episodio.append((pnl_medio, fav_medio, adv_medio))
    stats_episodio = _resumen(valores_episodio)

    return stats_señal, stats_episodio
