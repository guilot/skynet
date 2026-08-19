"""Carga y validación de la configuración del scanner."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from scanner_volumen.models import State


@dataclass(frozen=True)
class MarketConfig:
    venue: str


@dataclass(frozen=True)
class UniverseConfig:
    """Filtro de universo en dos etapas (ver `Orchestrator._admite_libro`):
    `min_volume_24h` es el prefiltro barato que acota cuántos símbolos
    llegan a pedir 14 días de histórico; `min_profile_median_volume` es la
    puerta real -el volumen 24h no distingue "libro sostenible" de "dos
    ráfagas y silencio" (medido: HUSDT reporta $10M de 24h con una mediana
    de minuto de $57), así que una vez existe el perfil de un símbolo, es
    su `typical_volume()` -no su volumen 24h- quien decide si se queda en
    el universo activo."""

    min_volume_24h: float
    min_profile_median_volume: float
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
class BacktestConfig:
    """Umbrales de negocio de la herramienta de backtest (requisitos 2 y 3):
    el hueco que colapsa señales consecutivas del mismo símbolo en un
    episodio, el número mínimo de episodios por debajo del cual un
    resultado se marca como no significativo, y el corte de timestamp que
    separa las señales puntuadas con la penalización de VWAP anterior."""

    episode_gap_minutes: float
    min_episodes_for_significance: int
    score_change_cutoff_ts: int


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
    backtest: BacktestConfig


# Los tres bloques del score (MOMENTUM 6 + DEMAND 4 + STRUCTURE 3, ver
# `scoring/score.py::score_symbol`). Viven aquí y no en score.py -que los
# importa de vuelta- para que `CURVAS_ESPERADAS`, justo abajo, pueda
# derivarse de las mismas tuplas en vez de mantener una cuarta lista con los
# mismos 13 nombres escrita a mano (Minor: antes eran dos copias
# independientes; una curva nueva en un bloque de score.py sin añadirla
# también aquí habría quedado sin validar en el arranque). `score.py` ya
# importa de `config.py` para `ScoreConfig`/`ScoreCurve`, así que esto no
# invierte la dirección de dependencia.
CLAVES_MOMENTUM = ("ret_1m", "ret_3m", "ret_5m", "ret_15m", "ret_1h", "ret_24h")
CLAVES_DEMAND = ("rvol_1m", "rvol_5m", "rvol_session", "demand_burst")
CLAVES_STRUCTURE = ("vwap", "z_return", "market_cap")

# Las 13 curvas que `score_symbol` necesita para puntuar los tres bloques de
# arriba. `score_symbol` filtra con `if clave in cfg.curves`, así que una
# curva ausente en el TOML anularía en silencio esa dimensión del score sin
# ningún error: se valida aquí, al cargar, en vez de dejar que falle en
# silencio en producción.
CURVAS_ESPERADAS = frozenset((*CLAVES_MOMENTUM, *CLAVES_DEMAND, *CLAVES_STRUCTURE))

_ESTADOS_VALIDOS = {s.value for s in State}


def _validar_backtest(raw_backtest: dict) -> None:
    """Valida los umbrales de `[backtest]` al cargar, igual que ya hacen las
    curvas y los estados (Minor): sin esto, un `episode_gap_minutes = 0` en
    el TOML no fallaba hasta `episodes.group_episodes`, en tiempo de
    ejecución del backtest y sin contexto sobre qué campo del TOML lo causó,
    en vez de fallar aquí, en un único punto al arrancar."""
    gap = raw_backtest["episode_gap_minutes"]
    if gap <= 0:
        raise ValueError(
            f"backtest.episode_gap_minutes debe ser positivo, llegó {gap!r}"
        )
    minimo = raw_backtest["min_episodes_for_significance"]
    if minimo < 0:
        raise ValueError(
            "backtest.min_episodes_for_significance no puede ser negativo, "
            f"llegó {minimo!r}"
        )


def _validar_estado(campo: str, valor: str) -> None:
    """Valida que `valor` sea un `State` real, en un único punto de fallo
    al cargar la config (Minor). Sin esto, un typo en `alert_min_state` o
    `persisted_min_state` no fallaba hasta construir `StateMachine` u
    `Orchestrator` -bien dentro del arranque de la app, con un
    `ValueError` de `State(...)` sin ningún contexto sobre qué campo del
    TOML lo causó- en vez de fallar aquí, igual que ya hacen las curvas."""
    if valor not in _ESTADOS_VALIDOS:
        raise ValueError(
            f"{campo} no es un State válido: {valor!r} "
            f"(válidos: {sorted(_ESTADOS_VALIDOS)})"
        )


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

    _validar_estado("states.alert_min_state", raw["states"]["alert_min_state"])
    _validar_estado(
        "orchestrator.persisted_min_state", raw["orchestrator"]["persisted_min_state"]
    )
    _validar_backtest(raw["backtest"])

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
        backtest=BacktestConfig(**raw["backtest"]),
    )
