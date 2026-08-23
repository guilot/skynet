from scanner_volumen.backtest.segmentation import (
    LEGACY_ANTES_DEL_CORTE,
    LEGACY_DESDE_EL_CORTE,
    compute_segmentation,
)
from scanner_volumen.provenance import PRE_PROVENANCE_SENTINEL

MINUTO = 60_000
FP_A = "a" * 64
FP_B = "b" * 64


def señal(id_, symbol, ts, config_fingerprint=FP_A):
    return {"id": id_, "symbol": symbol, "ts": ts, "config_fingerprint": config_fingerprint}


def test_agrupa_por_fingerprint_no_por_un_corte_elegido_a_mano():
    """El caso que reemplaza al corte manual: dos fingerprints distintos
    producen dos grupos, sin que nadie tenga que identificar a mano ningún
    timestamp de corte."""
    señales = [
        señal(1, "AAA", 100, FP_A),
        señal(2, "AAA", 200, FP_A),
        señal(3, "BBB", 300, FP_B),
        señal(4, "BBB", 400, FP_B),
    ]
    grupos = compute_segmentation(señales, gap_minutes=30)
    etiquetas = {g.label: g for g in grupos}
    assert set(etiquetas) == {FP_A, FP_B}
    assert etiquetas[FP_A].n_signals == 2
    assert etiquetas[FP_B].n_signals == 2


def test_cuenta_episodios_por_separado_dentro_de_cada_grupo():
    # dos señales del mismo símbolo con el mismo fingerprint, separadas por
    # un hueco grande: deben seguir siendo un único grupo (mismo
    # fingerprint), y sus episodios se cuentan dentro de ESE grupo.
    señales = [
        señal(1, "AAA", 0, FP_A),
        señal(2, "AAA", 100 * MINUTO, FP_A),
    ]
    grupos = compute_segmentation(señales, gap_minutes=30)
    assert len(grupos) == 1
    assert grupos[0].n_episodes == 2  # el hueco de 100 min separa los episodios


def test_lista_vacia_no_lanza():
    assert compute_segmentation([], gap_minutes=30) == ()


def test_grupos_ordenados_por_ts_min_ascendente():
    señales = [
        señal(1, "AAA", 500, FP_B),
        señal(2, "BBB", 0, FP_A),
    ]
    grupos = compute_segmentation(señales, gap_minutes=30)
    assert [g.label for g in grupos] == [FP_A, FP_B]


def test_filas_sin_config_fingerprint_caen_en_el_centinela():
    """Un dict de señal que no trae la clave (defensivo, p. ej. una fuente
    de datos antigua en un test) se trata igual que el centinela explícito,
    nunca se agrupa por accidente con un fingerprint real."""
    señales = [{"id": 1, "symbol": "AAA", "ts": 0}]
    grupos = compute_segmentation(señales, gap_minutes=30)
    assert len(grupos) == 1
    assert grupos[0].label == PRE_PROVENANCE_SENTINEL


def test_sin_corte_legado_las_filas_centinela_quedan_en_un_unico_grupo():
    señales = [
        señal(1, "AAA", 0, PRE_PROVENANCE_SENTINEL),
        señal(2, "AAA", 100 * MINUTO, PRE_PROVENANCE_SENTINEL),
    ]
    grupos = compute_segmentation(señales, gap_minutes=30)
    assert len(grupos) == 1
    assert grupos[0].label == PRE_PROVENANCE_SENTINEL
    assert grupos[0].n_signals == 2


def test_corte_legado_parte_solo_las_filas_centinela_en_dos_grupos():
    """El caso real: las 285 filas de producción migradas comparten el
    centinela y no se pueden distinguir por fingerprint entre sí, pero SÍ
    contienen la frontera del endurecimiento de la curva VWAP que se
    identificó a mano (config.toml, score_change_cutoff_ts). Ese corte debe
    seguir partiendo ESE grupo -y solo ese-, nunca uno con fingerprint real."""
    señales = [
        señal(1, "AAA", 100, PRE_PROVENANCE_SENTINEL),
        señal(2, "AAA", 2000, PRE_PROVENANCE_SENTINEL),  # en/después del corte
        señal(3, "BBB", 3000, FP_A),  # fingerprint real: nunca se parte
        señal(4, "BBB", 4000, FP_A),
    ]
    grupos = compute_segmentation(señales, gap_minutes=30, legacy_cutoff_ts=1000)
    etiquetas = {g.label: g for g in grupos}
    assert set(etiquetas) == {LEGACY_ANTES_DEL_CORTE, LEGACY_DESDE_EL_CORTE, FP_A}
    assert etiquetas[LEGACY_ANTES_DEL_CORTE].n_signals == 1
    assert etiquetas[LEGACY_DESDE_EL_CORTE].n_signals == 1
    assert etiquetas[FP_A].n_signals == 2  # intacto: no se tocó por el corte legado


def test_corte_legado_es_exclusivo_del_lado_antes():
    """Igual que el `SignalRepo.pending_outcomes`/comportamiento previo: una
    fila con `ts == legacy_cutoff_ts` cuenta como "desde el corte", no como
    "antes"."""
    señales = [señal(1, "AAA", 1000, PRE_PROVENANCE_SENTINEL)]
    grupos = compute_segmentation(señales, gap_minutes=30, legacy_cutoff_ts=1000)
    assert len(grupos) == 1
    assert grupos[0].label == LEGACY_DESDE_EL_CORTE
