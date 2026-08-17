# scanner_volumen/scoring/score.py
"""Score 0-100 con reparto MOMENTUM 40 / DEMAND 40 / STRUCTURE 20.

Cada componente se calcula con una función lineal por tramos definida en la
configuración, nunca con constantes en el código: recalibrar en V3 debe ser
editar el TOML.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from scanner_volumen.config import (
    CLAVES_DEMAND, CLAVES_MOMENTUM, CLAVES_STRUCTURE, ScoreConfig, ScoreCurve,
)
from scanner_volumen.engine.metrics import SymbolMetrics
from scanner_volumen.models import Direction

# CLAVES_MOMENTUM/DEMAND/STRUCTURE viven en config.py (Minor), no aquí: así
# `config.CURVAS_ESPERADAS` se deriva de las mismas tuplas que usa
# `score_symbol` más abajo, en vez de mantener una lista aparte con los
# mismos 13 nombres escrita a mano. Se re-exportan con el import de arriba
# para no romper a quien ya las importaba desde este módulo.


def piecewise(value: float | None, curve: ScoreCurve) -> float:
    """Interpola linealmente entre breakpoints.

    Los breakpoints pueden ir en orden ascendente o descendente del valor de
    entrada (descendente para market cap, donde menos puntúa más), y la curva
    puede no ser monótona (el caso del VWAP, que penaliza en ambos extremos).
    En ambos casos el eje X de los breakpoints es monótono; lo que varía es
    el eje Y.
    """
    if value is None:
        return 0.0
    puntos = curve.breakpoints
    if not puntos:
        return 0.0

    ascendente = puntos[-1][0] >= puntos[0][0]
    if ascendente:
        if value <= puntos[0][0]:
            return puntos[0][1]
        if value >= puntos[-1][0]:
            return puntos[-1][1]
    else:
        if value >= puntos[0][0]:
            return puntos[0][1]
        if value <= puntos[-1][0]:
            return puntos[-1][1]

    for (x0, y0), (x1, y1) in zip(puntos, puntos[1:]):
        dentro = (x0 <= value <= x1) if ascendente else (x1 <= value <= x0)
        if dentro:
            if x1 == x0:
                return y1
            frac = (value - x0) / (x1 - x0)
            return y0 + frac * (y1 - y0)
    return puntos[-1][1]


def detect_direction(
    ret_5m: float | None, vwap_distance: float | None
) -> Direction:
    if ret_5m is None or vwap_distance is None:
        return Direction.NEUTRAL
    if ret_5m > 0 and vwap_distance > 0:
        return Direction.LONG
    if ret_5m < 0 and vwap_distance < 0:
        return Direction.SHORT
    return Direction.NEUTRAL


@dataclass(frozen=True)
class ScoreBreakdown:
    total: float          # acotado a 0-100, con el multiplicador NEUTRAL ya aplicado
    raw_total: float      # momentum + demand + structure, sin acotar ni atenuar
    momentum: float
    demand: float
    structure: float
    direction: Direction
    components: dict[str, float] = field(default_factory=dict)


def _con_signo(valor: float | None, direccion: Direction) -> float | None:
    """En SHORT, un -3% debe puntuar como un +3% en LONG."""
    if valor is None:
        return None
    return -valor if direccion is Direction.SHORT else valor


def score_symbol(metrics: SymbolMetrics, cfg: ScoreConfig) -> ScoreBreakdown:
    direccion = detect_direction(metrics.ret_5m, metrics.vwap_distance)

    entradas: dict[str, float | None] = {
        "ret_1m": _con_signo(metrics.ret_1m, direccion),
        "ret_3m": _con_signo(metrics.ret_3m, direccion),
        "ret_5m": _con_signo(metrics.ret_5m, direccion),
        "ret_15m": _con_signo(metrics.ret_15m, direccion),
        "ret_1h": _con_signo(metrics.ret_1h, direccion),
        "ret_24h": _con_signo(metrics.ret_24h, direccion),
        "rvol_1m": metrics.rvol_1m_closed,
        "rvol_5m": metrics.rvol_5m,
        "rvol_session": metrics.rvol_session,
        "demand_burst": metrics.demand_burst,
        # el z-score se toma en valor absoluto: importa la magnitud del
        # movimiento, y la dirección ya la fija `direccion`
        "z_return": abs(metrics.z_return) if metrics.z_return is not None else None,
        "market_cap": metrics.market_cap,
        "vwap": _con_signo(metrics.vwap_distance, direccion),
    }

    componentes = {
        clave: piecewise(valor, cfg.curves[clave])
        for clave, valor in entradas.items()
        if clave in cfg.curves
    }

    momentum = sum(componentes.get(k, 0.0) for k in CLAVES_MOMENTUM)
    demand = sum(componentes.get(k, 0.0) for k in CLAVES_DEMAND)
    structure = sum(componentes.get(k, 0.0) for k in CLAVES_STRUCTURE)

    # raw_total es la suma pura de los tres bloques: nunca lleva el
    # multiplicador NEUTRAL ni el acotado a 0-100. Es lo que le permite al
    # dashboard mostrar por qué una moneda no llegó a `signal` incluso cuando
    # `total` ya se recortó a 0.
    raw_total = momentum + demand + structure
    ajustado = raw_total * cfg.neutral_multiplier if direccion is Direction.NEUTRAL else raw_total

    return ScoreBreakdown(
        total=max(0.0, min(100.0, ajustado)),
        raw_total=raw_total,
        momentum=momentum,
        demand=demand,
        structure=structure,
        direction=direccion,
        components=componentes,
    )
