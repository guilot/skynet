"""Reglas de entrada a comparar (spec: estado mínimo, dirección, distancia
opcional a VWAP)."""
from __future__ import annotations

from dataclasses import dataclass

from scanner_volumen.models import State

DIRECCIONES_VALIDAS = ("ALL", "LONG", "SHORT")


@dataclass(frozen=True)
class EntryRule:
    """Una combinación de filtros de entrada a evaluar contra el histórico.

    `min_state` se compara por `rank` (State.rank), no por igualdad: una
    regla "mínimo SIGNAL" también admite EXTREME, que escala por encima.
    `direction` "ALL" no filtra por dirección (admite también NEUTRAL).
    `max_vwap_distance`, si se da, exige `abs(vwap_distance) <= valor`; una
    señal sin ese dato se excluye -nunca se asume que pasa el filtro-.

    `fade`, si es `True`, invierte la operación: la señal original decide
    QUÉ señales califican (estado, dirección, vwap_distance se evalúan
    siempre sobre la señal tal cual fue grabada), pero el P&L se calcula
    sobre la posición contraria -un LONG se fadea yendo SHORT y viceversa-.
    Ver `stats.compute_combo_stats` para la negación/intercambio real.
    """

    label: str
    min_state: State
    direction: str = "ALL"
    max_vwap_distance: float | None = None
    fade: bool = False

    def __post_init__(self) -> None:
        if self.direction not in DIRECCIONES_VALIDAS:
            raise ValueError(
                f"direction inválida: {self.direction!r} (válidas: {DIRECCIONES_VALIDAS})"
            )

    def matches(
        self, *, state: str, direction: str, vwap_distance: float | None
    ) -> bool:
        try:
            estado = State(state)
        except ValueError:
            return False
        if estado.rank < self.min_state.rank:
            return False
        if self.direction != "ALL" and direction != self.direction:
            return False
        if self.max_vwap_distance is not None:
            if vwap_distance is None or abs(vwap_distance) > self.max_vwap_distance:
                return False
        return True


def default_entry_rules() -> tuple[EntryRule, ...]:
    """La matriz de comparación por defecto: los tres estados persistidos
    (HOT/SIGNAL/EXTREME, ver `orchestrator.persisted_min_state`) por las tres
    direcciones, más dos variantes que ilustran el filtro opcional de
    distancia a VWAP sobre la regla más laxa (SIGNAL+/ALL), más las tres
    variantes FADE de HOT+ (ver docstring de `EntryRule.fade`): el análisis
    sobre 243 episodios mostró que la señal del scanner tiene edge negativo
    en su propia dirección -entra en el agotamiento del momentum, y el
    precio revierte-, así que fadearla (tomar la posición contraria) es la
    hipótesis que este informe existe para medir."""
    reglas: list[EntryRule] = []
    for estado in (State.HOT, State.SIGNAL, State.EXTREME):
        for direccion in DIRECCIONES_VALIDAS:
            reglas.append(
                EntryRule(
                    label=f"{estado.value}+ / {direccion}",
                    min_state=estado,
                    direction=direccion,
                )
            )
    for distancia in (5.0, 10.0):
        reglas.append(
            EntryRule(
                label=f"SIGNAL+ / ALL / |vwap_dist|<={distancia:g}",
                min_state=State.SIGNAL,
                direction="ALL",
                max_vwap_distance=distancia,
            )
        )
    for direccion in DIRECCIONES_VALIDAS:
        reglas.append(
            EntryRule(
                label=f"FADE {State.HOT.value}+ / {direccion}",
                min_state=State.HOT,
                direction=direccion,
                fade=True,
            )
        )
    return tuple(reglas)
