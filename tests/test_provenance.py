# tests/test_provenance.py
"""Procedencia de las señales: fingerprint de configuración y revisión de
código (ver `scanner_volumen/provenance.py`).

Estos dos casos son los que un bug silencioso dañaría más -por eso van
primero, antes de tocar `storage/db.py` u `orchestrator.py` (TDD)-:

- el fingerprint NO debe cambiar entre `config.toml` y `config.dev.toml`
  (solo difieren en `server.port`/`server.db_path`, ver
  `tests/test_config.py::test_config_dev_solo_difiere_de_produccion_en_db_path_y_puerto`);
  si cambiara, cada arranque en modo dev crearía una frontera falsa en el
  dataset.
- SÍ debe cambiar si una curva de score cambia, para que el error real que
  motivó esta tarea (identificar a mano, y mal por 17 h, el corte del
  endurecimiento de la curva VWAP) no pueda volver a pasar.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scanner_volumen.config import load_config
from scanner_volumen.provenance import (
    PRE_PROVENANCE_SENTINEL,
    UNKNOWN_REVISION,
    config_fingerprint,
    get_code_revision,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"
CONFIG_DEV_PATH = CONFIG_PATH.parent / "config.dev.toml"


def test_el_centinela_de_pre_procedencia_no_parece_un_fingerprint_real():
    # un sha256 real son 64 hex; el centinela debe ser imposible de confundir.
    assert len(PRE_PROVENANCE_SENTINEL) != 64
    assert PRE_PROVENANCE_SENTINEL != UNKNOWN_REVISION  # significan cosas distintas


def test_fingerprint_es_identico_entre_config_toml_y_config_dev_toml():
    """config.toml y config.dev.toml solo difieren en server.port/db_path
    (invariante ya fijada en test_config.py). Si el fingerprint cambiara al
    arrancar con --config config.dev.toml, cada corrida de desarrollo
    crearía una frontera falsa de "cambio de scoring" en el dataset."""
    produccion = load_config(CONFIG_PATH)
    dev = load_config(CONFIG_DEV_PATH)
    assert config_fingerprint(produccion) == config_fingerprint(dev)


def test_fingerprint_es_determinista_y_estable_ante_reformateo():
    """El mismo config.toml, cargado dos veces, produce el mismo fingerprint
    -y no depende de texto crudo (comentarios/espaciado), solo de los valores
    ya parseados."""
    a = config_fingerprint(load_config(CONFIG_PATH))
    b = config_fingerprint(load_config(CONFIG_PATH))
    assert a == b
    assert isinstance(a, str) and len(a) == 64  # sha256 hexdigest


def test_fingerprint_cambia_si_una_curva_de_score_cambia(tmp_path):
    """El caso real que motivó esta tarea: endurecer una curva de score debe
    dejar rastro automático, no depender de identificar el corte a mano."""
    original = CONFIG_PATH.read_text()
    modificado = original.replace(
        'ret_1m       = { max_points = 4,  breakpoints = [[0.0, 0], [0.3, 2], [0.8, 4]] }',
        'ret_1m       = { max_points = 4,  breakpoints = [[0.0, 0], [0.3, 2], [0.9, 4]] }',
    )
    assert modificado != original  # guarda contra un replace que no encontró nada
    destino = tmp_path / "config_modificado.toml"
    destino.write_text(modificado)

    original_fp = config_fingerprint(load_config(CONFIG_PATH))
    modificado_fp = config_fingerprint(load_config(destino))
    assert original_fp != modificado_fp


def test_fingerprint_cambia_si_cambia_el_umbral_de_persistencia(tmp_path):
    """`persisted_min_state` decide QUÉ FILAS llegan a la tabla (hoy, HOT o
    más). Si cambiara, las filas de antes y las de después vendrían de
    poblaciones distintas y cualquier estadística sobre el conjunto tendría
    sesgo de selección -el backtest calcularía la regla `HOT+` sobre un tramo
    en el que las filas HOT ni siquiera se guardaban-. La huella responde
    "¿son comparables estas filas?", así que esto tiene que moverla."""
    original = CONFIG_PATH.read_text()
    modificado = original.replace(
        'persisted_min_state = "HOT"', 'persisted_min_state = "SIGNAL"'
    )
    assert modificado != original  # guarda contra un replace que no encontró nada
    destino = tmp_path / "config_persistencia.toml"
    destino.write_text(modificado)

    assert config_fingerprint(load_config(CONFIG_PATH)) != config_fingerprint(
        load_config(destino)
    )


def test_fingerprint_cambia_si_cambia_el_venue_del_mercado(tmp_path):
    """M1: `market.venue` (spot vs USDT-perp) cambia el universo de
    instrumentos entero -una incomparabilidad más fuerte que la que ya
    justificó arrastrar `gap_tolerance_minutes`-, así que debe mover el
    fingerprint igual que un cambio de curva de score o de umbral de
    persistencia."""
    original = CONFIG_PATH.read_text()
    modificado = original.replace(
        'venue = "USDT-FUTURES"', 'venue = "SPOT"',
    )
    assert modificado != original  # guarda contra un replace que no encontró nada
    destino = tmp_path / "config_venue.toml"
    destino.write_text(modificado)

    original_fp = config_fingerprint(load_config(CONFIG_PATH))
    modificado_fp = config_fingerprint(load_config(destino))
    assert original_fp != modificado_fp


def test_fingerprint_no_cambia_si_solo_cambian_ajustes_operativos(tmp_path):
    """server (host/puerto/db_path), dashboard (stale_after_seconds) y
    maintenance (interval_hours) son ajustes operativos: no gobiernan qué se
    detecta ni cómo se puntúa, así que cambiarlos no debe crear una frontera
    falsa en el dataset."""
    original = CONFIG_PATH.read_text()
    modificado = (
        original
        .replace("port = 8000", "port = 9999")
        .replace('db_path = "data/scanner.db"', 'db_path = "data/otra.db"')
        .replace("stale_after_seconds = 30", "stale_after_seconds = 999")
        .replace("interval_hours = 24", "interval_hours = 1")
    )
    assert modificado != original
    destino = tmp_path / "config_operativo.toml"
    destino.write_text(modificado)

    original_fp = config_fingerprint(load_config(CONFIG_PATH))
    modificado_fp = config_fingerprint(load_config(destino))
    assert original_fp == modificado_fp


def test_fingerprint_ignora_el_orden_de_las_claves(tmp_path):
    """Reordenar las curvas de score en el TOML (equivalente a un
    reformateo) no debe cambiar el fingerprint: solo importan los valores."""
    original = CONFIG_PATH.read_text()
    bloque_orig = (
        'ret_1m       = { max_points = 4,  breakpoints = [[0.0, 0], [0.3, 2], [0.8, 4]] }\n'
        'ret_3m       = { max_points = 6,  breakpoints = [[0.0, 0], [0.8, 3], [2.0, 6]] }\n'
    )
    bloque_reordenado = (
        'ret_3m       = { max_points = 6,  breakpoints = [[0.0, 0], [0.8, 3], [2.0, 6]] }\n'
        'ret_1m       = { max_points = 4,  breakpoints = [[0.0, 0], [0.3, 2], [0.8, 4]] }\n'
    )
    assert bloque_orig in original
    modificado = original.replace(bloque_orig, bloque_reordenado)
    destino = tmp_path / "config_reordenado.toml"
    destino.write_text(modificado)

    original_fp = config_fingerprint(load_config(CONFIG_PATH))
    modificado_fp = config_fingerprint(load_config(destino))
    assert original_fp == modificado_fp


# --- code_revision ---


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _preparar_repo_git(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "archivo.txt").write_text("v1")
    _git(repo, "add", "archivo.txt")
    _git(repo, "commit", "-q", "-m", "inicial")
    return repo


def test_get_code_revision_devuelve_el_hash_corto_en_un_arbol_limpio(tmp_path):
    repo = _preparar_repo_git(tmp_path)
    rev = get_code_revision(cwd=repo)
    assert rev != UNKNOWN_REVISION
    assert "-dirty" not in rev
    # hash corto de git: hex, típicamente 7-12 caracteres
    assert all(c in "0123456789abcdef" for c in rev)


def test_get_code_revision_marca_el_arbol_sucio(tmp_path):
    repo = _preparar_repo_git(tmp_path)
    (repo / "archivo.txt").write_text("v2 sin commitear")

    rev = get_code_revision(cwd=repo)
    assert rev.endswith("-dirty")


def test_get_code_revision_degrada_si_no_hay_repositorio_git(tmp_path):
    """Un despliegue desde un tarball (sin `.git`) no debe romper el
    arranque: debe degradar a UNKNOWN_REVISION."""
    vacio = tmp_path / "sin_git"
    vacio.mkdir()
    assert get_code_revision(cwd=vacio) == UNKNOWN_REVISION


def test_get_code_revision_degrada_si_el_binario_git_no_existe(monkeypatch, tmp_path):
    """Sin el binario git instalado (FileNotFoundError al lanzar el
    subproceso), debe degradar en vez de lanzar."""
    def _lanza_no_encontrado(*args, **kwargs):
        raise FileNotFoundError("git no está en PATH")

    monkeypatch.setattr(subprocess, "run", _lanza_no_encontrado)
    assert get_code_revision(cwd=tmp_path) == UNKNOWN_REVISION
