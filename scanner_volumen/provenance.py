# scanner_volumen/provenance.py
"""Procedencia de cada señal: qué configuración y qué revisión de código la
produjeron.

Cada cambio en la puntuación o en la detección crea silenciosamente una
frontera en el dataset, y hasta ahora nada la registraba: cuando se endureció
la curva de extensión sobre VWAP (commit e1937d8), el corte tuvo que
identificarse a mano inspeccionando huecos de señales y timestamps de commit
-y el primer intento se equivocó por 17 h (ver config.toml,
`[backtest].score_change_cutoff_ts`)-. Este módulo hace que cada fila de
`signals` pueda decir por sí sola qué la produjo, en vez de depender de que
alguien la reconstruya después.

Ver `storage/db.py` (columnas `config_fingerprint`/`code_revision` de
`signals`, y la migración que las añade a una base ya existente) y
`backtest/segmentation.py` (agrupa el histórico por estos valores en vez de
por el corte de timestamp elegido a mano que existía antes).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path

from scanner_volumen.config import Config

# Centinela para señales grabadas ANTES de que existieran estas columnas
# (migración `_migrar_v2_procedencia_de_signals` en storage/db.py: las 285
# filas de producción ya existentes, mezcla sin marcar de corridas locales y
# de producción). Un string que nunca puede coincidir con un fingerprint
# sha256 real (siempre 64 caracteres hex) ni con una revisión git real, para
# que una fila migrada sea inconfundible con una de procedencia genuina.
PRE_PROVENANCE_SENTINEL = "pre-provenance"

# Centinela para cuando, en tiempo de ejecución, no se pudo determinar la
# revisión de git (sin binario git, despliegue desde un tarball, clon sin
# `.git`...). Semánticamente distinto de PRE_PROVENANCE_SENTINEL: una señal
# con este valor SÍ tiene un config_fingerprint real -solo falta saber qué
# revisión de código corría-, mientras que PRE_PROVENANCE_SENTINEL marca una
# fila de antes de que este sistema existiera.
UNKNOWN_REVISION = "unknown"

_SUFIJO_ARBOL_SUCIO = "-dirty"

# Las secciones de `Config` que gobiernan QUÉ se detecta y CÓMO se puntúa:
# las curvas de score, los umbrales de estado (WATCH/HOT/SIGNAL/EXTREME,
# exit_margin/exit_ticks/cooldown), los parámetros del motor (ventanas de
# z-score, lookback de burst...), los filtros de universo (volumen mínimo,
# max_symbols...) y los parámetros del perfil de volumen (history_days,
# smoothing...). Deliberadamente EXCLUYE las secciones puramente operativas:
# server (host/puerto/db_path), dashboard (stale_after_seconds), maintenance
# (interval_hours), rest (rate limiting), supply (cadencia de refresco de
# market cap), outcomes (cadencia del tracker) y backtest (cómo se agrupa un
# episodio, no qué ni cómo se puntúa). Sin esta exclusión, arrancar con
# config.dev.toml en vez de config.toml -que solo difieren en
# server.port/server.db_path, ver tests/test_config.py- crearía una frontera
# falsa de "cambio de scoring" en cuanto se guardara la primera señal de una
# corrida de desarrollo.
# `orchestrator` entra porque `persisted_min_state` decide QUE FILAS llegan a
# la tabla (hoy, HOT o mas). Si ese umbral cambiara, las filas de antes y las
# de despues vendrian de poblaciones distintas y toda estadistica sobre el
# conjunto tendria sesgo de seleccion: el backtest calcularia la regla `HOT+`
# sobre un tramo en el que las filas HOT ni siquiera se guardaban. La huella
# no responde "cambio el scoring?" sino "son comparables estas filas?", y esto
# ultimo si las hace incomparables. Arrastra `gap_tolerance_minutes`, que es
# operativo y podria crear alguna frontera de mas; se acepta porque los dos
# errores no cuestan igual: una frontera de mas parte los datos de forma
# visible y recuperable, una de menos los mezcla en silencio.
# `profile.stale_after_hours` (Finding "perfil rancio al entrar") es un
# umbral de TIMING -cuánto tarda un perfil rancio en reconstruirse al
# entrar un símbolo al universo-, pero se deja arrastrar dentro de
# `profile` a propósito, no se separa a una sección operativa aparte: a
# diferencia de `maintenance.interval_hours` (excluido, puramente de
# cadencia), este umbral decide DIRECTAMENTE qué baseline -el denominador
# real del RVOL- termina usando un símbolo recién reingresado. Dos
# corridas con distinto `stale_after_hours` producirían RVOL distintos
# para las mismas velas en esa ventana de entrada; no marcar una frontera
# ahí sería mezclar en silencio filas calculadas con reglas de frescura
# distintas, exactamente el riesgo que este módulo existe para evitar.
# `market` entra por el mismo criterio de "son comparables estas filas?"
# (Finding M1): `venue` (spot vs USDT-perp) no ajusta el scoring, cambia el
# universo de instrumentos entero -precios, volumenes y volatilidad de un
# perp no son el mismo activo estadistico que los del spot correspondiente-,
# una incomparabilidad mas fuerte que la que ya justifico arrastrar
# `gap_tolerance_minutes`. Omitirlo era la inconsistencia: un cambio de venue
# mezclaria en silencio filas de dos poblaciones distintas bajo el mismo
# fingerprint, exactamente el error que el modulo dice preferir evitar sobre
# el de crear una frontera de mas.
_SECCIONES_FINGERPRINT = (
    "market", "score", "states", "engine", "universe", "profile", "orchestrator",
)


def config_fingerprint(cfg: Config) -> str:
    """Hash estable de las secciones de `cfg` que determinan qué se detecta y
    cómo se puntúa, o que de otro modo hacen incomparables dos filas de
    `signals` (ver `_SECCIONES_FINGERPRINT` arriba para el porqué de
    exactamente estas siete y no las demás).

    Determinista y ajeno al formato del TOML de origen: se calcula sobre los
    valores ya parseados (`dataclasses.asdict`), serializados con
    `sort_keys=True`, así que reordenar tablas, añadir comentarios o
    reformatear `config.toml` no cambia el resultado -solo cambiar un valor
    real lo hace-.
    """
    subconjunto = {
        nombre: asdict(getattr(cfg, nombre)) for nombre in _SECCIONES_FINGERPRINT
    }
    canonico = json.dumps(subconjunto, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def get_code_revision(cwd: Path | str | None = None) -> str:
    """Hash corto de `git HEAD`, con sufijo `-dirty` si el árbol de trabajo
    tiene cambios sin commitear -sin eso, la revisión sola no describe
    completamente qué código produjo la señal-.

    Degrada a `UNKNOWN_REVISION` en vez de lanzar si git no está disponible:
    sin binario git instalado (`FileNotFoundError`), sin repositorio (código
    de salida distinto de cero, p. ej. un despliegue desde un tarball o un
    clon sin `.git`), o cualquier otro fallo del subproceso. Un arranque
    nunca debe fallar solo porque no se pudo determinar la revisión.
    """
    try:
        resultado = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN_REVISION

    revision = resultado.stdout.strip()
    if resultado.returncode != 0 or not revision:
        return UNKNOWN_REVISION

    sucio = _arbol_de_trabajo_sucio(cwd)
    return f"{revision}{_SUFIJO_ARBOL_SUCIO}" if sucio else revision


def _arbol_de_trabajo_sucio(cwd: Path | str | None) -> bool:
    """`True` si `git status --porcelain` reporta algún cambio sin
    commitear. Si el propio chequeo falla, se asume limpio -en el peor caso
    se pierde el sufijo `-dirty`, no se rompe el arranque por no poder
    determinarlo-."""
    try:
        estado = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return estado.returncode == 0 and bool(estado.stdout.strip())
