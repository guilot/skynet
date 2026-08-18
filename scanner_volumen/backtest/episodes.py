"""Agrupación de señales consecutivas del mismo símbolo en episodios.

Requisito 2 (el más importante): 43 señales medidas en real, de las cuales
36 eran el mismo símbolo a lo largo de cinco horas. Tratarlas como 43
observaciones independientes está mal: son un único episodio muestreado 36
veces, y cualquier media ingenua queda dominada por él. Este módulo colapsa
señales del mismo símbolo separadas por menos de `gap_minutes` en un único
episodio; el recuento de episodios (no de señales) es la muestra real.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Episode:
    """Una racha de señales consecutivas del mismo símbolo."""

    symbol: str
    signal_ids: tuple[int, ...]
    start_ts: int
    end_ts: int


def group_episodes(signals: list[dict], gap_minutes: float) -> list[Episode]:
    """Agrupa `signals` (cada una un dict con al menos "id", "symbol", "ts")
    por símbolo, y dentro de cada símbolo colapsa en un episodio toda racha
    de señales consecutivas cronológicamente cuyo hueco entre timestamps
    sucesivos no supere `gap_minutes`. Un hueco EXACTAMENTE igual al límite
    configurado sigue contando como el mismo episodio ("dentro de un hueco
    configurable"). No asume que `signals` venga ya ordenada ni agrupada."""
    if gap_minutes <= 0:
        raise ValueError(f"gap_minutes debe ser positivo, llegó {gap_minutes!r}")
    gap_ms = gap_minutes * 60_000

    por_simbolo: dict[str, list[dict]] = {}
    for s in signals:
        por_simbolo.setdefault(s["symbol"], []).append(s)

    episodios: list[Episode] = []
    for symbol, filas in por_simbolo.items():
        ordenadas = sorted(filas, key=lambda f: f["ts"])
        racha: list[dict] = []
        for fila in ordenadas:
            if racha and fila["ts"] - racha[-1]["ts"] > gap_ms:
                episodios.append(_construir_episodio(symbol, racha))
                racha = []
            racha.append(fila)
        if racha:
            episodios.append(_construir_episodio(symbol, racha))
    return episodios


def _construir_episodio(symbol: str, filas: list[dict]) -> Episode:
    return Episode(
        symbol=symbol,
        signal_ids=tuple(f["id"] for f in filas),
        start_ts=filas[0]["ts"],
        end_ts=filas[-1]["ts"],
    )
