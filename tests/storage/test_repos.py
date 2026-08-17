import sqlite3

import pytest

from scanner_volumen.engine.metrics import SymbolMetrics
from scanner_volumen.engine.profile import SlotStats, VolumeProfile
from scanner_volumen.models import Candle, Direction, State
from scanner_volumen.scoring.score import ScoreBreakdown
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import (
    CandleRepo, ProfileRepo, SignalRepo, SupplyRepo,
)

MINUTO = 60_000


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "test.db")
    yield c
    c.close()


def vela(ts, close=100.0, vol=10.0):
    return Candle(ts=ts, open=close, high=close, low=close, close=close,
                  base_vol=vol, quote_vol=vol * close)


def test_guarda_y_recupera_velas(conn):
    repo = CandleRepo(conn)
    repo.save_many("AAAUSDT", [vela(m * MINUTO) for m in range(5)])
    recuperadas = repo.load("AAAUSDT", since_ms=0)
    assert len(recuperadas) == 5
    assert recuperadas[0].ts == 0
    assert recuperadas[-1].ts == 4 * MINUTO


def test_guardar_la_misma_vela_dos_veces_la_actualiza(conn):
    repo = CandleRepo(conn)
    repo.save_many("AAAUSDT", [vela(0, vol=10)])
    repo.save_many("AAAUSDT", [vela(0, vol=99)])
    recuperadas = repo.load("AAAUSDT", since_ms=0)
    assert len(recuperadas) == 1
    assert recuperadas[0].base_vol == 99


def test_latest_ts_devuelve_el_ultimo_timestamp(conn):
    repo = CandleRepo(conn)
    assert repo.latest_ts("AAAUSDT") is None
    repo.save_many("AAAUSDT", [vela(m * MINUTO) for m in range(5)])
    assert repo.latest_ts("AAAUSDT") == 4 * MINUTO


def test_prune_borra_las_velas_antiguas(conn):
    repo = CandleRepo(conn)
    repo.save_many("AAAUSDT", [vela(m * MINUTO) for m in range(10)])
    repo.prune(older_than_ms=5 * MINUTO)
    restantes = repo.load("AAAUSDT", since_ms=0)
    # no basta con contar: hay que comprobar que las que sobreviven son las
    # nuevas (ts >= 5*MINUTO), no las viejas, para no dejar pasar una condición
    # invertida que borrase por casualidad la misma cantidad de velas.
    assert [c.ts for c in restantes] == [m * MINUTO for m in range(5, 10)]


def test_guarda_y_recupera_un_perfil(conn):
    repo = ProfileRepo(conn)
    slots = tuple(
        SlotStats(median=100.0 + m, p75=110.0, p90=120.0, p95=130.0, samples=210)
        if m < 3 else None
        for m in range(1440)
    )
    perfil = VolumeProfile("AAAUSDT", slots, confidence="high", days_covered=14.0)
    repo.save(perfil, now_ms=1_000)

    cargado = repo.load("AAAUSDT")
    assert cargado is not None
    assert cargado.confidence == "high"
    assert cargado.days_covered == 14.0
    assert cargado.slots[0].median == 100.0
    assert cargado.slots[2].median == 102.0
    assert cargado.slots[500] is None


def test_load_de_un_perfil_inexistente_devuelve_none(conn):
    assert ProfileRepo(conn).load("NOEXISTE") is None


def test_save_estampa_el_now_ms_real_y_lo_refresca_en_cada_guardado(conn):
    """Regresión I4: antes, `save` escribía el literal 0 en `updated_ms` y el
    `ON CONFLICT` no lo actualizaba, así que ni siquiera había un timestamp
    del que detectar que un perfil calculado en el arranque llevaba semanas
    sin recalcularse. Debe quedar el `now_ms` real, y debe seguir
    refrescándose en guardados posteriores del mismo símbolo, no solo en el
    primero."""
    repo = ProfileRepo(conn)
    slots = tuple(None for _ in range(1440))
    perfil = VolumeProfile("AAAUSDT", slots, confidence="low", days_covered=0.0)

    repo.save(perfil, now_ms=1_000)
    fila = conn.execute(
        "SELECT updated_ms FROM profile_meta WHERE symbol = ?", ("AAAUSDT",)
    ).fetchone()
    assert fila["updated_ms"] == 1_000

    repo.save(perfil, now_ms=2_000)
    fila = conn.execute(
        "SELECT updated_ms FROM profile_meta WHERE symbol = ?", ("AAAUSDT",)
    ).fetchone()
    assert fila["updated_ms"] == 2_000  # el ON CONFLICT sí lo actualizó


def metricas_de_prueba(**kwargs):
    base = dict(
        symbol="XYZUSDT", price=6.72, ret_1m=0.82, ret_3m=1.91, ret_5m=3.7,
        ret_15m=5.1, ret_30m=6.22, ret_1h=7.2, ret_24h=13.8,
        rvol_1m_closed=7.8, rvol_1m_live=8.1, rvol_5m=5.1, rvol_session=3.9,
        demand_burst=2.4, vwap=6.43, vwap_distance=4.5, z_return=3.2,
        market_cap=8e7, volume_24h=3.24e7, open_interest=1000.0,
        funding_rate=0.0001, profile_confidence="high", ts=1_000_000,
    )
    base.update(kwargs)
    return SymbolMetrics(**base)


def desglose():
    return ScoreBreakdown(total=89.0, raw_total=89.0, momentum=35.0, demand=37.0,
                          structure=17.0, direction=Direction.LONG,
                          components={"rvol_1m": 13.5})


def test_inserta_una_senal_con_todas_sus_metricas(conn):
    repo = SignalRepo(conn)
    signal_id = repo.insert(metricas_de_prueba(), desglose(), State.SIGNAL)
    assert signal_id > 0

    filas = repo.recent(since_ms=0)
    assert len(filas) == 1
    fila = filas[0]
    # se comprueban TODOS los campos de la instantánea, no solo unos pocos:
    # el diseño exige que se persista la foto completa de métricas, y un test
    # que solo mirase dos o tres campos dejaría pasar una implementación que
    # olvidase, intercambiase o truncase el resto.
    assert fila["symbol"] == "XYZUSDT"
    assert fila["ts"] == 1_000_000
    assert fila["direction"] == "LONG"
    assert fila["state"] == "SIGNAL"
    assert fila["score"] == 89.0
    assert fila["score_momentum"] == 35.0
    assert fila["score_demand"] == 37.0
    assert fila["score_structure"] == 17.0
    assert fila["price"] == 6.72
    assert fila["rvol_1m_closed"] == 7.8
    assert fila["rvol_1m_live"] == 8.1
    assert fila["rvol_5m"] == 5.1
    assert fila["rvol_session"] == 3.9
    assert fila["demand_burst"] == 2.4
    assert fila["ret_1m"] == 0.82
    assert fila["ret_3m"] == 1.91
    assert fila["ret_5m"] == 3.7
    assert fila["ret_15m"] == 5.1
    assert fila["ret_30m"] == 6.22
    assert fila["ret_1h"] == 7.2
    assert fila["ret_24h"] == 13.8
    assert fila["vwap"] == 6.43
    assert fila["vwap_distance"] == 4.5
    assert fila["z_return"] == 3.2
    assert fila["market_cap"] == 8e7
    assert fila["volume_24h"] == 3.24e7
    assert fila["open_interest"] == 1000.0
    assert fila["funding_rate"] == 0.0001
    assert fila["profile_confidence"] == "high"


def test_recent_filtra_por_timestamp(conn):
    repo = SignalRepo(conn)
    repo.insert(metricas_de_prueba(ts=1000), desglose(), State.SIGNAL)
    repo.insert(metricas_de_prueba(ts=9000), desglose(), State.SIGNAL)
    assert len(repo.recent(since_ms=5000)) == 1


def test_metricas_nulas_se_guardan_como_null(conn):
    # Una métrica almacenada como 0 en lugar de NULL es indistinguible de un
    # verdadero cero, lo que corrupta la calibración de puntuación en V3.
    # Este test verifica que TODA métrica nullable persiste como NULL.
    repo = SignalRepo(conn)
    repo.insert(
        metricas_de_prueba(
            price=None,
            rvol_1m_closed=None, rvol_1m_live=None, rvol_5m=None,
            rvol_session=None, demand_burst=None,
            ret_1m=None, ret_3m=None, ret_5m=None, ret_15m=None,
            ret_30m=None, ret_1h=None, ret_24h=None,
            vwap=None, vwap_distance=None, z_return=None,
            market_cap=None, volume_24h=None, open_interest=None,
            funding_rate=None,
        ),
        desglose(),
        State.HOT
    )
    fila = repo.recent(since_ms=0)[0]
    # Se verifica por nombre para detectar si cambia el orden de _CAMPOS
    assert fila["price"] is None
    assert fila["rvol_1m_closed"] is None
    assert fila["rvol_1m_live"] is None
    assert fila["rvol_5m"] is None
    assert fila["rvol_session"] is None
    assert fila["demand_burst"] is None
    assert fila["ret_1m"] is None
    assert fila["ret_3m"] is None
    assert fila["ret_5m"] is None
    assert fila["ret_15m"] is None
    assert fila["ret_30m"] is None
    assert fila["ret_1h"] is None
    assert fila["ret_24h"] is None
    assert fila["vwap"] is None
    assert fila["vwap_distance"] is None
    assert fila["z_return"] is None
    assert fila["market_cap"] is None
    assert fila["volume_24h"] is None
    assert fila["open_interest"] is None
    assert fila["funding_rate"] is None


def test_pending_outcomes_lista_los_horizontes_vencidos(conn):
    repo = SignalRepo(conn)
    sid = repo.insert(metricas_de_prueba(ts=0), desglose(), State.SIGNAL)
    # a los 6 minutos han vencido los horizontes de 1 y 5 minutos
    pendientes = repo.pending_outcomes(now_ms=6 * MINUTO, horizons=(1, 5, 15, 30, 60))
    horizontes = sorted(h for _, _, _, h, _ in pendientes)
    assert horizontes == [1, 5]
    assert all(s == sid for s, _, _, _, _ in pendientes)


def test_pending_outcomes_incluye_el_limite_exacto_de_vencimiento(conn):
    """El horizonte vence en cuanto transcurre exactamente ese número de
    minutos, no solo cuando ya se ha superado: a los 5 minutos justos el
    horizonte de 5 minutos ya debe estar pendiente. Este caso límite no lo
    cubre el test anterior (6 minutos supera a ambos horizontes con margen),
    así que una implementación con un off-by-one en el filtro de vencimiento
    lo pasaría igualmente si no fuera por este test."""
    repo = SignalRepo(conn)
    sid = repo.insert(metricas_de_prueba(ts=0), desglose(), State.SIGNAL)
    pendientes = repo.pending_outcomes(now_ms=5 * MINUTO, horizons=(1, 5, 15, 30, 60))
    horizontes = sorted(h for _, _, _, h, _ in pendientes)
    assert horizontes == [1, 5]
    # y justo un milisegundo antes del vencimiento, el horizonte de 5 minutos
    # todavía no debe aparecer
    pendientes_antes = repo.pending_outcomes(
        now_ms=5 * MINUTO - 1, horizons=(1, 5, 15, 30, 60)
    )
    assert sorted(h for _, _, _, h, _ in pendientes_antes) == [1]


def test_pending_outcomes_no_confunde_el_resultado_de_otro_horizonte(conn):
    """Guardar el resultado del horizonte de 1 minuto no debe tapar el
    horizonte de 5 minutos de la misma señal: cada horizonte se resuelve por
    separado. Una implementación que comprobase "existe algún resultado para
    esta señal" en vez de "existe el resultado de ESTE horizonte" pasaría el
    test `test_un_outcome_guardado_deja_de_estar_pendiente` de casualidad
    (porque ahí solo se guarda un horizonte), pero fallaría aquí."""
    repo = SignalRepo(conn)
    sid = repo.insert(metricas_de_prueba(ts=0), desglose(), State.SIGNAL)
    repo.save_outcome(sid, horizon_min=1, price=7.0, return_pct=4.2,
                      mfe_pct=5.0, mae_pct=-0.5, candles_seen=2, candles_expected=2)
    pendientes = repo.pending_outcomes(now_ms=60 * MINUTO, horizons=(1, 5, 15, 30, 60))
    horizontes = sorted(h for _, _, _, h, _ in pendientes)
    assert horizontes == [5, 15, 30, 60]


def test_un_outcome_guardado_deja_de_estar_pendiente(conn):
    repo = SignalRepo(conn)
    sid = repo.insert(metricas_de_prueba(ts=0), desglose(), State.SIGNAL)
    repo.save_outcome(sid, horizon_min=1, price=7.0, return_pct=4.2,
                      mfe_pct=5.0, mae_pct=-0.5, candles_seen=2, candles_expected=2)
    pendientes = repo.pending_outcomes(now_ms=6 * MINUTO, horizons=(1, 5, 15, 30, 60))
    assert sorted(h for _, _, _, h, _ in pendientes) == [5]


def test_save_outcome_persiste_la_completitud_de_la_ventana(conn):
    """Una ventana calculada sobre un hueco (candles_seen < candles_expected)
    debe distinguirse en el propio dato persistido de una completa, no solo
    en la lógica que la produjo: si `save_outcome` ignorara estos dos
    argumentos, esta lectura devolvería NULL o los valores de otra fila."""
    repo = SignalRepo(conn)
    sid = repo.insert(metricas_de_prueba(ts=0), desglose(), State.SIGNAL)
    repo.save_outcome(sid, horizon_min=5, price=7.0, return_pct=4.2,
                      mfe_pct=5.0, mae_pct=-0.5, candles_seen=4, candles_expected=6)
    fila = conn.execute(
        "SELECT candles_seen, candles_expected FROM signal_outcomes "
        "WHERE signal_id = ? AND horizon_min = 5", (sid,),
    ).fetchone()
    assert fila["candles_seen"] == 4
    assert fila["candles_expected"] == 6


def test_supply_repo_hace_upsert(conn):
    repo = SupplyRepo(conn)
    repo.upsert("BTCUSDT", "bitcoin", 19_800_000, 1.2e12, 1.3e12, updated_ms=0)
    repo.upsert("BTCUSDT", "bitcoin", 19_900_000, 1.3e12, 1.4e12, updated_ms=1000)
    caps = repo.load_all()
    assert caps["BTCUSDT"] == 1.3e12
