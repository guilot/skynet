"""Selección del universo de símbolos a escanear.

Los símbolos que dejan de cumplir los filtros no se descartan de inmediato:
disponen de un periodo de gracia para evitar perder el histórico de un símbolo
que oscila alrededor del umbral de volumen.
"""
from __future__ import annotations

from dataclasses import dataclass

from scanner_volumen.config import UniverseConfig
from scanner_volumen.models import Contract, Ticker


@dataclass(frozen=True)
class UniverseUpdate:
    symbols: frozenset[str]
    added: frozenset[str]
    removed: frozenset[str]
    ordered: list[str]  # por volumen 24h descendente: prioridad de bootstrap


class UniverseSelector:
    def __init__(self, cfg: UniverseConfig) -> None:
        self._cfg = cfg
        self._current: frozenset[str] = frozenset()
        # símbolo -> instante de la última vez que cumplió los filtros
        self._last_qualified_ms: dict[str, int] = {}

    def select(
        self, contracts: list[Contract], tickers: list[Ticker], now_ms: int
    ) -> UniverseUpdate:
        aptos = {
            c.symbol
            for c in contracts
            if c.status == "normal"
            and c.symbol_type == "perpetual"
            and not (self._cfg.exclude_rwa and c.is_rwa)
        }

        candidatos = [
            t
            for t in tickers
            if t.symbol in aptos and t.volume_24h_usdt >= self._cfg.min_volume_24h
        ]
        candidatos.sort(key=lambda t: t.volume_24h_usdt, reverse=True)
        califican = {t.symbol for t in candidatos[: self._cfg.max_symbols]}

        gracia_ms = self._cfg.exit_grace_minutes * 60_000
        for simbolo in califican:
            self._last_qualified_ms[simbolo] = now_ms

        retenidos: set[str] = set()
        expirados: set[str] = set()
        for simbolo in self._current - califican:
            ultima_vez = self._last_qualified_ms.get(simbolo, now_ms)
            if now_ms - ultima_vez < gracia_ms:
                retenidos.add(simbolo)
            else:
                expirados.add(simbolo)
                self._last_qualified_ms.pop(simbolo, None)

        nuevos = frozenset(califican - self._current)
        activos = frozenset(califican | retenidos)

        orden = [t.symbol for t in candidatos if t.symbol in activos]
        orden += sorted(activos - set(orden))

        self._current = activos
        return UniverseUpdate(
            symbols=activos,
            added=nuevos,
            removed=frozenset(expirados),
            ordered=orden,
        )
