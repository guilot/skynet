# scanner_volumen/app/bootstrap.py
"""Descarga del histórico de velas y construcción de perfiles de volumen.

Es la parte cara del arranque: 14 días de velas de 1m son ~101 peticiones por
símbolo, porque history-candles no acepta más de 200 velas por llamada. Se
persiste en SQLite para que un reinicio solo tenga que rellenar el hueco.
"""
from __future__ import annotations

import logging

from scanner_volumen.config import ProfileConfig
from scanner_volumen.engine.profile import VolumeProfile, build_profile
from scanner_volumen.storage.repos import CandleRepo, ProfileRepo

MINUTO_MS = 60_000
DIA_MS = 1440 * MINUTO_MS

log = logging.getLogger(__name__)


def plan_history_requests(
    latest_ts: int | None, now_ms: int, history_days: int, page_size: int = 200
) -> list[int]:
    """Devuelve los `endTime` a solicitar, del más reciente al más antiguo.

    Si ya hay histórico, solo planifica el hueco entre `latest_ts` y ahora.
    `latest_ts` es el timestamp de apertura de la última vela ya guardada;
    esa vela ya cubre su propio minuto, así que el hueco empieza en el
    minuto siguiente (`latest_ts + MINUTO_MS`), no en `latest_ts` mismo: de
    lo contrario un histórico al día seguiría pidiendo una página de sobra.
    """
    inicio_deseado = now_ms - history_days * DIA_MS
    if latest_ts is None:
        desde = inicio_deseado
    else:
        desde = max(inicio_deseado, latest_ts + MINUTO_MS)
    minutos = max(0, (now_ms - desde) // MINUTO_MS)
    if minutos == 0:
        return []
    paginas = -(-minutos // page_size)  # división entera hacia arriba
    return [now_ms - i * page_size * MINUTO_MS for i in range(paginas)]


class Bootstrapper:
    """Orquesta la descarga de histórico y la construcción del perfil de
    volumen por símbolo. `expect`/`progress` existen para que el arrancador
    pueda mostrar avance mientras recorre el universo (~25 min en frío)."""

    def __init__(
        self,
        rest,
        candle_repo: CandleRepo,
        profile_repo: ProfileRepo,
        profile_cfg: ProfileConfig,
    ) -> None:
        self._rest = rest
        self._candles = candle_repo
        self._profiles = profile_repo
        self._cfg = profile_cfg
        self._esperados: set[str] = set()
        self._completados: set[str] = set()

    def expect(self, symbols: list[str]) -> None:
        self._esperados.update(symbols)

    def progress(self) -> tuple[int, int]:
        return len(self._completados), len(self._esperados or self._completados)

    def mark_loaded(self, symbol: str) -> None:
        """Cuenta como completado un símbolo cuyo perfil ya existía en disco.

        En un arranque en caliente, `ensure_profile` carga el perfil
        directamente de `ProfileRepo` sin pasar por `bootstrap_symbol`, que es
        el único sitio que hasta ahora engordaba `_completados`. Sin esto,
        `progress()` se quedaría en 0/N para siempre aunque no falte nada por
        descargar.
        """
        self._esperados.add(symbol)
        self._completados.add(symbol)

    async def bootstrap_symbol(self, symbol: str, now_ms: int) -> VolumeProfile:
        self._esperados.add(symbol)
        paginas = plan_history_requests(
            self._candles.latest_ts(symbol), now_ms, self._cfg.history_days
        )

        for end_time in paginas:
            try:
                velas = await self._rest.get_history_candles(symbol, end_time_ms=end_time)
            except Exception as exc:  # noqa: BLE001 - una página perdida no aborta el símbolo
                log.warning("página de histórico fallida para %s en %d: %s",
                            symbol, end_time, exc)
                continue
            if velas:
                self._candles.save_many(symbol, velas)

        desde = now_ms - self._cfg.history_days * DIA_MS
        perfil = build_profile(symbol, self._candles.load(symbol, desde), self._cfg)
        self._profiles.save(perfil, now_ms)
        self._completados.add(symbol)
        return perfil
