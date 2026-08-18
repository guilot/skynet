from scanner_volumen.backtest.episodes import group_episodes

MINUTO = 60_000


def señal(id_, symbol, ts):
    return {"id": id_, "symbol": symbol, "ts": ts}


def test_36_senales_del_mismo_simbolo_en_cinco_horas_colapsan_en_un_episodio():
    """Requisito 2, el caso que motiva la herramienta: 43 señales medidas en
    real, 36 del mismo símbolo repartidas a lo largo de cinco horas. Con el
    hueco por defecto (30 min) y separaciones de ~8 min entre señales
    consecutivas, deben colapsar en un único episodio -no 36 observaciones
    independientes-."""
    inicio = 0
    paso = 5 * 60_000 // 36  # ~8.3 min, reparte 36 señales en 5 horas
    señales = [señal(i, "TUTUSDT", inicio + i * paso) for i in range(36)]

    episodios = group_episodes(señales, gap_minutes=30)

    assert len(episodios) == 1
    assert episodios[0].symbol == "TUTUSDT"
    assert len(episodios[0].signal_ids) == 36
    assert episodios[0].signal_ids == tuple(range(36))


def test_un_hueco_mayor_al_configurado_separa_dos_episodios():
    señales = [
        señal(1, "AAA", 0),
        señal(2, "AAA", 10 * MINUTO),
        # hueco de 40 min > 30 min configurados: nuevo episodio
        señal(3, "AAA", 50 * MINUTO),
        señal(4, "AAA", 55 * MINUTO),
    ]
    episodios = group_episodes(señales, gap_minutes=30)
    assert len(episodios) == 2
    assert episodios[0].signal_ids == (1, 2)
    assert episodios[1].signal_ids == (3, 4)


def test_un_hueco_igual_al_limite_configurado_no_separa():
    # "dentro de un hueco configurable" (por defecto 30 min): el límite
    # exacto debe seguir contando como el mismo episodio, no romperlo.
    señales = [señal(1, "AAA", 0), señal(2, "AAA", 30 * MINUTO)]
    episodios = group_episodes(señales, gap_minutes=30)
    assert len(episodios) == 1


def test_simbolos_distintos_nunca_se_mezclan_en_el_mismo_episodio():
    señales = [
        señal(1, "AAA", 0),
        señal(2, "BBB", 60_000),  # mismo instante, símbolo distinto
    ]
    episodios = group_episodes(señales, gap_minutes=30)
    assert len(episodios) == 2
    simbolos = {e.symbol for e in episodios}
    assert simbolos == {"AAA", "BBB"}


def test_agrupa_por_simbolo_aunque_las_senales_lleguen_desordenadas_en_el_tiempo():
    señales = [
        señal(1, "AAA", 10 * MINUTO),
        señal(2, "BBB", 0),
        señal(3, "AAA", 0),
    ]
    episodios = group_episodes(señales, gap_minutes=30)
    aaa = next(e for e in episodios if e.symbol == "AAA")
    # ordenado cronológicamente dentro del episodio, no en el orden de entrada
    assert aaa.signal_ids == (3, 1)
    assert aaa.start_ts == 0
    assert aaa.end_ts == 10 * MINUTO


def test_lista_vacia_no_produce_episodios():
    assert group_episodes([], gap_minutes=30) == []


def test_una_sola_senal_es_su_propio_episodio():
    episodios = group_episodes([señal(1, "AAA", 0)], gap_minutes=30)
    assert len(episodios) == 1
    assert episodios[0].signal_ids == (1,)
    assert episodios[0].start_ts == episodios[0].end_ts == 0
