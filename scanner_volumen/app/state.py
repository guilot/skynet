# scanner_volumen/app/state.py
"""Estado compartido que el dashboard consulta sin recalcular nada."""
from __future__ import annotations

from dataclasses import dataclass, field

from scanner_volumen.engine.metrics import SymbolMetrics
from scanner_volumen.models import State
from scanner_volumen.scoring.score import ScoreBreakdown


@dataclass(frozen=True)
class SymbolSnapshot:
    symbol: str
    metrics: SymbolMetrics
    breakdown: ScoreBreakdown
    state: State
    updated_ms: int

    def to_dict(self) -> dict:
        m = self.metrics
        return {
            "symbol": self.symbol,
            "price": m.price,
            "direction": self.breakdown.direction.value,
            "state": self.state.value,
            "score": round(self.breakdown.total, 1),
            "score_momentum": round(self.breakdown.momentum, 1),
            "score_demand": round(self.breakdown.demand, 1),
            "score_structure": round(self.breakdown.structure, 1),
            "components": {k: round(v, 2) for k, v in self.breakdown.components.items()},
            "ret_1m": m.ret_1m, "ret_5m": m.ret_5m, "ret_15m": m.ret_15m,
            "ret_1h": m.ret_1h, "ret_24h": m.ret_24h,
            "rvol_1m": m.rvol_1m_closed, "rvol_1m_live": m.rvol_1m_live,
            "rvol_5m": m.rvol_5m, "rvol_session": m.rvol_session,
            "demand_burst": m.demand_burst,
            "vwap": m.vwap, "vwap_distance": m.vwap_distance,
            "z_return": m.z_return, "market_cap": m.market_cap,
            "volume_24h": m.volume_24h,
            "profile_confidence": m.profile_confidence,
            "updated_ms": self.updated_ms,
        }


@dataclass
class ScannerState:
    snapshots: dict[str, SymbolSnapshot] = field(default_factory=dict)
    connected: bool = False
    bootstrap_done: int = 0
    bootstrap_total: int = 0

    def put(self, snapshot: SymbolSnapshot) -> None:
        self.snapshots[snapshot.symbol] = snapshot

    def drop(self, symbol: str) -> None:
        self.snapshots.pop(symbol, None)

    def snapshot(self, symbol: str) -> SymbolSnapshot | None:
        return self.snapshots.get(symbol)

    def ranked(self) -> list[SymbolSnapshot]:
        return sorted(
            self.snapshots.values(), key=lambda s: s.breakdown.total, reverse=True
        )

    def to_dict(self) -> dict:
        return {
            "connected": self.connected,
            "bootstrap": {"done": self.bootstrap_done, "total": self.bootstrap_total},
            "rows": [s.to_dict() for s in self.ranked()],
        }
