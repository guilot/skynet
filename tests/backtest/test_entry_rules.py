import pytest

from scanner_volumen.backtest.entry_rules import (
    DIRECCIONES_VALIDAS, EntryRule, default_entry_rules,
)
from scanner_volumen.models import State


def test_matches_exige_estado_minimo_por_rank_no_por_igualdad():
    regla = EntryRule(label="x", min_state=State.SIGNAL, direction="ALL")
    # EXTREME (rank 4) escala por encima de SIGNAL (rank 3): debe calificar
    # igual que si fuera exactamente SIGNAL.
    assert regla.matches(state="EXTREME", direction="LONG", vwap_distance=None)
    assert regla.matches(state="SIGNAL", direction="LONG", vwap_distance=None)
    assert not regla.matches(state="HOT", direction="LONG", vwap_distance=None)


def test_matches_filtra_por_direccion():
    regla_long = EntryRule(label="x", min_state=State.HOT, direction="LONG")
    assert regla_long.matches(state="HOT", direction="LONG", vwap_distance=None)
    assert not regla_long.matches(state="HOT", direction="SHORT", vwap_distance=None)

    regla_todas = EntryRule(label="x", min_state=State.HOT, direction="ALL")
    assert regla_todas.matches(state="HOT", direction="LONG", vwap_distance=None)
    assert regla_todas.matches(state="HOT", direction="SHORT", vwap_distance=None)
    assert regla_todas.matches(state="HOT", direction="NEUTRAL", vwap_distance=None)


def test_matches_aplica_el_filtro_opcional_de_distancia_a_vwap():
    regla = EntryRule(label="x", min_state=State.HOT, direction="ALL", max_vwap_distance=5.0)
    assert regla.matches(state="HOT", direction="LONG", vwap_distance=4.9)
    assert regla.matches(state="HOT", direction="LONG", vwap_distance=-4.9)
    assert not regla.matches(state="HOT", direction="LONG", vwap_distance=5.1)
    assert not regla.matches(state="HOT", direction="LONG", vwap_distance=-5.1)
    # sin dato de vwap_distance, no se puede evaluar el filtro: se excluye,
    # nunca se asume que pasa.
    assert not regla.matches(state="HOT", direction="LONG", vwap_distance=None)


def test_matches_ignora_un_estado_desconocido_en_vez_de_lanzar():
    regla = EntryRule(label="x", min_state=State.HOT, direction="ALL")
    assert not regla.matches(state="NO_ES_UN_ESTADO", direction="LONG", vwap_distance=None)


def test_direction_invalida_lanza_al_construir():
    with pytest.raises(ValueError):
        EntryRule(label="x", min_state=State.HOT, direction="LARGO")


def test_default_entry_rules_cubre_los_tres_estados_y_las_tres_direcciones():
    reglas = default_entry_rules()
    combinaciones = {(r.min_state, r.direction) for r in reglas if r.max_vwap_distance is None}
    esperado = {
        (estado, direccion)
        for estado in (State.HOT, State.SIGNAL, State.EXTREME)
        for direccion in ("ALL", "LONG", "SHORT")
    }
    assert combinaciones == esperado


def test_default_entry_rules_incluye_variantes_con_filtro_de_vwap():
    reglas = default_entry_rules()
    con_filtro = [r for r in reglas if r.max_vwap_distance is not None]
    assert con_filtro  # al menos una regla demuestra el filtro opcional


# --- fade ---------------------------------------------------------------

def test_entry_rule_fade_por_defecto_es_false():
    regla = EntryRule(label="x", min_state=State.HOT, direction="ALL")
    assert regla.fade is False


def test_matches_no_depende_de_fade_filtra_igual_sobre_la_senal_original():
    """El fade decide CÓMO se opera la señal, no CUÁLES señales califican:
    `matches` debe comportarse idéntico con fade=True o fade=False, porque
    los filtros de estado/dirección/vwap se evalúan siempre sobre la señal
    tal cual fue grabada, nunca sobre la operación invertida."""
    regla_normal = EntryRule(label="x", min_state=State.HOT, direction="SHORT")
    regla_fade = EntryRule(label="x", min_state=State.HOT, direction="SHORT", fade=True)
    for direccion in ("LONG", "SHORT", "NEUTRAL"):
        assert regla_normal.matches(
            state="HOT", direction=direccion, vwap_distance=None
        ) == regla_fade.matches(state="HOT", direction=direccion, vwap_distance=None)


def test_default_entry_rules_incluye_variantes_fade_de_hot_para_las_tres_direcciones():
    reglas = default_entry_rules()
    fade = [r for r in reglas if r.fade]
    assert {r.direction for r in fade} == set(DIRECCIONES_VALIDAS)
    assert all(r.min_state == State.HOT for r in fade)
    assert all(r.label.startswith("FADE") for r in fade)
