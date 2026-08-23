# tests/app/test_outcomes.py
import pytest

from scanner_volumen.app.outcomes import OutcomeTracker, compute_outcome
from scanner_volumen.engine.metrics import SymbolMetrics
from scanner_volumen.models import Candle, Direction, State
from scanner_volumen.scoring.score import ScoreBreakdown
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import CandleRepo, SignalRepo

MINUTO = 60_000
FP_PRUEBA = "e" * 64
REV_PRUEBA = "test-rev"


def vela(ts, close, high=None, low=None):
    return Candle(ts=ts, open=close, high=high or close, low=low or close,
                  close=close, base_vol=1.0, quote_vol=100.0)


def test_calcula_retorno_mfe_y_mae():
    velas = [
        vela(0 * MINUTO, 100.0, high=103.0, low=99.0),
        vela(1 * MINUTO, 104.0, high=108.0, low=101.0),
        vela(2 * MINUTO, 102.0, high=105.0, low=97.0),
    ]
    final, ret, mfe, mae = compute_outcome(entry_price=100.0, candles=velas)
    assert final == 102.0
    assert abs(ret - 2.0) < 1e-9
    assert abs(mfe - 8.0) < 1e-9   # máximo 108
    assert abs(mae + 3.0) < 1e-9   # mínimo 97


def test_sin_velas_devuelve_none():
    assert compute_outcome(entry_price=100.0, candles=[]) is None


def test_precio_de_entrada_no_positivo_devuelve_none():
    """El diseño exige que un precio de entrada inválido (cero o negativo)
    devuelva None en vez de lanzar o dividir por cero: un implementación que
    solo comprobase `candles` vacío y no el precio pasaría igual el resto de
    tests, pero fallaría (o lanzaría ZeroDivisionError) aquí."""
    velas = [vela(0 * MINUTO, 100.0)]
    assert compute_outcome(entry_price=0.0, candles=velas) is None
    assert compute_outcome(entry_price=-5.0, candles=velas) is None


@pytest.fixture
def repos(tmp_path):
    conn = open_db(tmp_path / "t.db")
    yield SignalRepo(conn), CandleRepo(conn)
    conn.close()


def metricas(ts, price):
    return SymbolMetrics(
        symbol="AAAUSDT", price=price, ret_1m=1.0, ret_3m=1.0, ret_5m=1.0,
        ret_15m=1.0, ret_30m=1.0, ret_1h=1.0, ret_24h=1.0,
        rvol_1m_closed=5.0, rvol_1m_live=5.0, rvol_5m=3.0, rvol_session=2.0,
        demand_burst=1.5, vwap=99.0, vwap_distance=1.0, z_return=2.0,
        market_cap=1e8, volume_24h=5e6, open_interest=1.0, funding_rate=0.0,
        profile_confidence="high", ts=ts,
    )


def desglose():
    return ScoreBreakdown(85.0, 85.0, 34.0, 34.0, 17.0, Direction.LONG, {})


def test_rellena_los_horizontes_vencidos(repos):
    signal_repo, candle_repo = repos
    signal_repo.insert(metricas(ts=0, price=100.0), desglose(), State.SIGNAL, config_fingerprint=FP_PRUEBA, code_revision=REV_PRUEBA)
    candle_repo.save_many("AAAUSDT", [vela(m * MINUTO, 100.0 + m) for m in range(0, 11)])

    tracker = OutcomeTracker(signal_repo, candle_repo, horizons=(1, 5, 15))
    escritos = tracker.run_once(now_ms=6 * MINUTO)

    assert escritos == 2  # horizontes 1 y 5; el de 15 aún no vence
    assert tracker.run_once(now_ms=6 * MINUTO) == 0  # ya no hay pendientes


def test_no_escribe_si_faltan_velas(repos):
    signal_repo, candle_repo = repos
    signal_repo.insert(metricas(ts=0, price=100.0), desglose(), State.SIGNAL, config_fingerprint=FP_PRUEBA, code_revision=REV_PRUEBA)
    tracker = OutcomeTracker(signal_repo, candle_repo, horizons=(1,))
    assert tracker.run_once(now_ms=6 * MINUTO) == 0


def test_no_escribe_horizonte_con_ventana_incompleta(repos):
    """El horizonte de 5 minutos ya venció según el reloj (pasaron 10
    minutos), pero solo llegaron velas hasta el minuto 3: la ventana está
    incompleta y no debe grabarse con lo que haya, hay que esperar a que
    lleguen las velas que faltan. Una implementación que solo filtrara
    `c.ts <= fin` sin comprobar que la vela del límite ya llegó grabaría
    igualmente un resultado (con menos velas de las debidas) y pasaría
    `test_rellena_los_horizontes_vencidos` de casualidad, porque ahí sí hay
    velas hasta el final de cada horizonte vencido."""
    signal_repo, candle_repo = repos
    signal_repo.insert(metricas(ts=0, price=100.0), desglose(), State.SIGNAL, config_fingerprint=FP_PRUEBA, code_revision=REV_PRUEBA)
    candle_repo.save_many("AAAUSDT", [vela(m * MINUTO, 100.0 + m) for m in range(0, 4)])

    tracker = OutcomeTracker(signal_repo, candle_repo, horizons=(5,))
    assert tracker.run_once(now_ms=10 * MINUTO) == 0

    fila = signal_repo._conn.execute(
        "SELECT COUNT(*) AS n FROM signal_outcomes"
    ).fetchone()
    assert fila["n"] == 0


def test_ts_no_alineado_al_minuto_se_redondea_y_registra_resultado(repos):
    """El orquestador evalúa cada segundo (`int(time.time() * 1000)`), así que
    el `ts` de una señal real casi nunca cae justo en el arranque de un
    minuto. Con ts=90_123 (minuto 1, segundo 30.123) la ventana debe
    alinearse al minuto de la vela vigente en ese instante para que el
    horizonte pueda completarse alguna vez: sin redondeo, `fin` nunca
    coincide con el ts de ninguna vela y el horizonte no se registra jamás."""
    signal_repo, candle_repo = repos
    ts_señal = 90_123
    signal_repo.insert(metricas(ts=ts_señal, price=100.0), desglose(), State.SIGNAL, config_fingerprint=FP_PRUEBA, code_revision=REV_PRUEBA)
    candle_repo.save_many("AAAUSDT", [
        vela(1 * MINUTO, 100.0, high=103.0, low=99.0),
        vela(2 * MINUTO, 104.0, high=108.0, low=101.0),
        vela(3 * MINUTO, 102.0, high=105.0, low=97.0),
    ])

    tracker = OutcomeTracker(signal_repo, candle_repo, horizons=(1,))
    escritos = tracker.run_once(now_ms=ts_señal + 5 * MINUTO)

    assert escritos == 1
    fila = signal_repo._conn.execute(
        "SELECT * FROM signal_outcomes WHERE horizon_min = 1"
    ).fetchone()
    assert fila is not None
    assert fila["price"] == 104.0  # cierre de la vela del minuto 2 (límite alineado)


def test_no_escribe_si_las_velas_no_llegan_al_limite_con_ts_no_alineado(repos):
    """Aunque `ts` no esté alineado al minuto, si el stream de velas todavía
    no progresó más allá del límite del horizonte (aquí se detiene justo un
    minuto antes) el horizonte sigue sin registrarse: la protección contra
    ventanas truncadas debe seguir vigente tras redondear `ts`."""
    signal_repo, candle_repo = repos
    ts_señal = 90_123
    signal_repo.insert(metricas(ts=ts_señal, price=100.0), desglose(), State.SIGNAL, config_fingerprint=FP_PRUEBA, code_revision=REV_PRUEBA)
    candle_repo.save_many("AAAUSDT", [
        vela(1 * MINUTO, 100.0),  # solo llega la vela de entrada (minuto 1)
    ])

    tracker = OutcomeTracker(signal_repo, candle_repo, horizons=(1,))
    assert tracker.run_once(now_ms=ts_señal + 5 * MINUTO) == 0

    fila = signal_repo._conn.execute(
        "SELECT COUNT(*) AS n FROM signal_outcomes"
    ).fetchone()
    assert fila["n"] == 0


def test_registra_con_hueco_interno_si_los_datos_llegan_mas_alla_del_limite(repos):
    """Un hueco real en mitad de la ventana (por ejemplo el exchange no
    publicó esas velas, ni siquiera la del límite exacto del horizonte) no
    debe bloquear el registro para siempre: en cuanto el stream de velas
    progresa más allá del límite, la ventana se da por completa con las
    velas que sí llegaron, en vez de esperar indefinidamente a una vela
    concreta que quizá nunca llegue."""
    signal_repo, candle_repo = repos
    signal_repo.insert(metricas(ts=0, price=100.0), desglose(), State.SIGNAL, config_fingerprint=FP_PRUEBA, code_revision=REV_PRUEBA)
    candle_repo.save_many("AAAUSDT", [
        vela(0 * MINUTO, 100.0, high=103.0, low=99.0),
        vela(1 * MINUTO, 104.0, high=108.0, low=101.0),
        # faltan los minutos 2 y 3 (hueco real, incluida la vela límite)
        vela(4 * MINUTO, 102.0),  # progresa más allá del límite del horizonte de 3 min
    ])

    tracker = OutcomeTracker(signal_repo, candle_repo, horizons=(3,))
    escritos = tracker.run_once(now_ms=6 * MINUTO)

    assert escritos == 1
    fila = signal_repo._conn.execute(
        "SELECT * FROM signal_outcomes WHERE horizon_min = 3"
    ).fetchone()
    assert fila["price"] == 104.0  # cierre de la última vela disponible dentro de la ventana (minuto 1)
    # ventana incompleta: faltan los minutos 2 y 3 (el propio límite del
    # horizonte). candles_expected = horizonte + 1 = 4 (minutos 0..3
    # inclusive); solo llegaron 2 (minutos 0 y 1). Sin registrar esta
    # completitud, esta fila sería indistinguible de una ventana entera.
    assert fila["candles_seen"] == 2
    assert fila["candles_expected"] == 4


def test_guarda_el_resultado_correcto_para_el_horizonte(repos):
    """La ventana del horizonte de 1 minuto debe ser [ts_señal, ts_señal +
    1min] inclusive en ambos extremos: incluye la vela de la propia señal
    (minuto 0) y la vela justo en el límite (minuto 1), pero no la vela del
    minuto 2, que cae fuera del horizonte. Comprobar los valores exactos
    (no solo el conteo) detecta tanto una ventana que se pasa del límite
    como una que excluye la vela de entrada."""
    signal_repo, candle_repo = repos
    signal_repo.insert(metricas(ts=0, price=100.0), desglose(), State.SIGNAL, config_fingerprint=FP_PRUEBA, code_revision=REV_PRUEBA)
    candle_repo.save_many("AAAUSDT", [
        vela(0 * MINUTO, 100.0, high=103.0, low=99.0),
        vela(1 * MINUTO, 104.0, high=108.0, low=101.0),
        vela(2 * MINUTO, 90.0, high=110.0, low=80.0),  # fuera de la ventana de 1 min
    ])

    tracker = OutcomeTracker(signal_repo, candle_repo, horizons=(1,))
    escritos = tracker.run_once(now_ms=2 * MINUTO)

    assert escritos == 1
    fila = signal_repo._conn.execute(
        "SELECT * FROM signal_outcomes WHERE horizon_min = 1"
    ).fetchone()
    assert fila["price"] == 104.0                # cierre de la vela límite (minuto 1)
    assert abs(fila["return_pct"] - 4.0) < 1e-9
    assert abs(fila["mfe_pct"] - 8.0) < 1e-9      # máximo 108 (minuto 0-1), no 110 del minuto 2
    assert abs(fila["mae_pct"] + 1.0) < 1e-9      # mínimo 99 (vela de entrada), no 80 del minuto 2
    # ventana completa: llegaron las 2 velas esperadas (minutos 0 y 1).
    assert fila["candles_seen"] == 2
    assert fila["candles_expected"] == 2
