"""Segmentación del histórico de señales por procedencia real
(`config_fingerprint`, ver `scanner_volumen/provenance.py`), en vez del corte
de timestamp elegido a mano que existía antes.

El corte a mano (`config.toml`, `[backtest].score_change_cutoff_ts`) tuvo que
identificarse inspeccionando huecos de señales y el timestamp del commit que
endureció la curva de VWAP -y el primer intento se equivocó por 17 h, porque
el proceso que corría en ese momento no recargó la config hasta su siguiente
reinicio-. Agrupar por el fingerprint que cada señal ya lleva estampado
elimina esa reconstrucción manual: dos señales con el mismo fingerprint se
puntuaron con exactamente la misma config; dos con fingerprints distintos,
no, sin importar cuándo se reinició el proceso.

Las 285 filas grabadas antes de que existiera esta columna comparten todas
el mismo centinela `PRE_PROVENANCE_SENTINEL` (`storage/db.py`), así que el
fingerprint no puede distinguir entre ellas -pero ese grupo SÍ contiene, sin
forma automática de saberlo, la única frontera de scoring que se identificó
a mano antes de que existiera esta migración. `legacy_cutoff_ts`, si se
pasa, parte SOLO ese grupo centinela en un antes/después; nunca toca filas
con un fingerprint real, que ya llevan su propia frontera automática."""
from __future__ import annotations

from dataclasses import dataclass

from scanner_volumen.backtest.episodes import group_episodes
from scanner_volumen.provenance import PRE_PROVENANCE_SENTINEL

# Etiquetas del grupo centinela partido por `legacy_cutoff_ts`. Nunca pueden
# coincidir con un config_fingerprint real (que es siempre un sha256 hex de
# 64 caracteres): ambas empiezan por PRE_PROVENANCE_SENTINEL seguido de ':'.
LEGACY_ANTES_DEL_CORTE = f"{PRE_PROVENANCE_SENTINEL}:antes-del-corte-legado"
LEGACY_DESDE_EL_CORTE = f"{PRE_PROVENANCE_SENTINEL}:desde-el-corte-legado"


@dataclass(frozen=True)
class ProvenanceGroup:
    """Un grupo de señales con la misma procedencia identificable.

    `label` es el `config_fingerprint` real compartido por el grupo, o -para
    las filas centinela, sin procedencia real- `PRE_PROVENANCE_SENTINEL` sin
    partir (si no se pasó `legacy_cutoff_ts`) o `LEGACY_ANTES_DEL_CORTE` /
    `LEGACY_DESDE_EL_CORTE` (si sí se pasó)."""

    label: str
    n_signals: int
    n_episodes: int
    ts_min: int
    ts_max: int


def compute_segmentation(
    signals: list[dict], gap_minutes: float, legacy_cutoff_ts: int | None = None,
) -> tuple[ProvenanceGroup, ...]:
    """Agrupa `signals` (dicts con al menos `ts` y `config_fingerprint`) por
    procedencia y agrupa los episodios de cada grupo por separado -un hueco
    que cruce dos procedencias distintas no debe unir un episodio de una con
    uno de otra, igual que antes lo evitaba el corte manual-.

    Devuelve los grupos ordenados por `ts_min` ascendente, para que el
    informe se lea como una línea de tiempo. Lista vacía si `signals` está
    vacío."""
    filas_por_etiqueta: dict[str, list[dict]] = {}
    for fila in signals:
        etiqueta = _etiqueta_de(fila, legacy_cutoff_ts)
        filas_por_etiqueta.setdefault(etiqueta, []).append(fila)

    grupos = [
        ProvenanceGroup(
            label=etiqueta,
            n_signals=len(filas),
            n_episodes=len(group_episodes(filas, gap_minutes)),
            ts_min=min(f["ts"] for f in filas),
            ts_max=max(f["ts"] for f in filas),
        )
        for etiqueta, filas in filas_por_etiqueta.items()
    ]
    grupos.sort(key=lambda g: g.ts_min)
    return tuple(grupos)


def _etiqueta_de(fila: dict, legacy_cutoff_ts: int | None) -> str:
    fingerprint = fila.get("config_fingerprint") or PRE_PROVENANCE_SENTINEL
    if fingerprint != PRE_PROVENANCE_SENTINEL or legacy_cutoff_ts is None:
        return fingerprint
    return (
        LEGACY_ANTES_DEL_CORTE if fila["ts"] < legacy_cutoff_ts
        else LEGACY_DESDE_EL_CORTE
    )
