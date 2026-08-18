from scanner_volumen.backtest.segmentation import compute_segmentation

MINUTO = 60_000


def señal(id_, symbol, ts):
    return {"id": id_, "symbol": symbol, "ts": ts}


def test_separa_senales_antes_y_desde_el_corte():
    señales = [
        señal(1, "AAA", 100),
        señal(2, "AAA", 999),
        señal(3, "BBB", 1000),  # exactamente en el corte: cuenta como "desde"
        señal(4, "BBB", 2000),
    ]
    split = compute_segmentation(señales, cutoff_ts=1000, gap_minutes=30)
    assert split.n_before == 2
    assert split.n_after == 2
    assert split.cutoff_ts == 1000


def test_cuenta_episodios_por_separado_en_cada_lado_del_corte():
    # dos señales del mismo símbolo, una a cada lado del corte, con hueco
    # grande entre ellas: cada lado debe agrupar sus episodios de forma
    # independiente, no mezclarlos a través del corte.
    señales = [
        señal(1, "AAA", 0),
        señal(2, "AAA", 100 * MINUTO),
    ]
    split = compute_segmentation(señales, cutoff_ts=50 * MINUTO, gap_minutes=30)
    assert split.episodes_before == 1
    assert split.episodes_after == 1


def test_lado_vacio_no_lanza():
    split = compute_segmentation([], cutoff_ts=1000, gap_minutes=30)
    assert split.n_before == 0
    assert split.n_after == 0
    assert split.episodes_before == 0
    assert split.episodes_after == 0
