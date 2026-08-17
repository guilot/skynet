"""Carga y validación de la configuración del scanner."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class MarketConfig:
    venue: str


@dataclass(frozen=True)
class UniverseConfig:
    min_volume_24h: float
    max_symbols: int
    exclude_rwa: bool
    refresh_minutes: int
    exit_grace_minutes: int


@dataclass(frozen=True)
class ProfileConfig:
    history_days: int
    smoothing_window_minutes: int
    min_days_for_confidence: int
    rolling_fallback_candles: int


@dataclass(frozen=True)
class EngineConfig:
    tick_seconds: float
    ticker_poll_seconds: float
    live_rvol_min_elapsed_seconds: float
    zscore_window_minutes: int
    zscore_min_samples: int
    burst_lookback_minutes: int
    demand_burst_min_denominator: float


@dataclass(frozen=True)
class RestConfig:
    rate_limit_per_second: float


@dataclass(frozen=True)
class MaintenanceConfig:
    """Cadencia de las tareas de mantenimiento diarias (I2 poda de velas, I4
    recálculo del perfil de volumen)."""

    interval_hours: float


@dataclass(frozen=True)
class DashboardConfig:
    """Umbral de obsolescencia (I1): a partir de cuántos segundos sin
    actualizarse una fila del dashboard se marca como potencialmente
    desconectada, aunque el badge de conexión general siga en verde."""

    stale_after_seconds: float


@dataclass(frozen=True)
class StatesConfig:
    watch: float
    hot: float
    signal: float
    extreme: float
    exit_margin: float
    exit_ticks: int
    cooldown_minutes: int
    alert_min_state: str


@dataclass(frozen=True)
class ScoreCurve:
    """Función lineal por tramos. Los breakpoints pueden ir en orden
    ascendente o descendente del valor de entrada."""

    max_points: float
    breakpoints: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class ScoreConfig:
    neutral_multiplier: float
    curves: dict[str, ScoreCurve] = field(default_factory=dict)


@dataclass(frozen=True)
class OrchestratorConfig:
    """Umbrales de negocio del orquestador: a partir de qué estado se
    persiste una señal en `signals`, y cuántos minutos de hueco tras una
    reconexión se toleran sin disparar un relleno por REST."""

    persisted_min_state: str
    gap_tolerance_minutes: int


@dataclass(frozen=True)
class SupplyConfig:
    """Cadencia de refresco de la cache de market cap contra CoinGecko
    (spec §4.3: `supply_refresher | 6 h`)."""

    refresh_hours: int


@dataclass(frozen=True)
class OutcomesConfig:
    """Horizontes (spec §9) y cadencia del tracker que rellena
    `signal_outcomes` (spec §4.3: `outcome_tracker | 1 min`)."""

    horizons_minutes: tuple[int, ...]
    poll_seconds: float


@dataclass(frozen=True)
class ServerConfig:
    """Host/puerto del dashboard y ruta de la base de datos SQLite."""

    host: str
    port: int
    db_path: str


@dataclass(frozen=True)
class Config:
    market: MarketConfig
    universe: UniverseConfig
    profile: ProfileConfig
    engine: EngineConfig
    rest: RestConfig
    states: StatesConfig
    score: ScoreConfig
    maintenance: MaintenanceConfig
    dashboard: DashboardConfig
    orchestrator: OrchestratorConfig
    supply: SupplyConfig
    outcomes: OutcomesConfig
    server: ServerConfig


# Las 13 curvas que `score_symbol` necesita para puntuar los tres bloques
# (MOMENTUM 6 + DEMAND 4 + STRUCTURE 3, ver scoring/score.py). `score_symbol`
# filtra con `if clave in cfg.curves`, así que una curva ausente en el TOML
# anularía en silencio esa dimensión del score sin ningún error: se valida
# aquí, al cargar, en vez de dejar que falle en silencio en producción.
CURVAS_ESPERADAS = frozenset({
    "ret_1m", "ret_3m", "ret_5m", "ret_15m", "ret_1h", "ret_24h",
    "rvol_1m", "rvol_5m", "rvol_session", "demand_burst",
    "z_return", "market_cap", "vwap",
})


def load_config(path: Path) -> Config:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    curves = {
        name: ScoreCurve(
            max_points=float(spec["max_points"]),
            breakpoints=tuple((float(x), float(y)) for x, y in spec["breakpoints"]),
        )
        for name, spec in raw["score"]["curves"].items()
    }

    faltantes = CURVAS_ESPERADAS - curves.keys()
    if faltantes:
        raise ValueError(
            f"faltan curvas de score en config.toml: {sorted(faltantes)}"
        )

    return Config(
        market=MarketConfig(**raw["market"]),
        universe=UniverseConfig(**raw["universe"]),
        profile=ProfileConfig(**raw["profile"]),
        engine=EngineConfig(**raw["engine"]),
        rest=RestConfig(**raw["rest"]),
        states=StatesConfig(**raw["states"]),
        score=ScoreConfig(
            neutral_multiplier=float(raw["score"]["neutral_multiplier"]),
            curves=curves,
        ),
        maintenance=MaintenanceConfig(**raw["maintenance"]),
        dashboard=DashboardConfig(**raw["dashboard"]),
        orchestrator=OrchestratorConfig(**raw["orchestrator"]),
        supply=SupplyConfig(**raw["supply"]),
        outcomes=OutcomesConfig(
            horizons_minutes=tuple(raw["outcomes"]["horizons_minutes"]),
            poll_seconds=float(raw["outcomes"]["poll_seconds"]),
        ),
        server=ServerConfig(**raw["server"]),
    )
