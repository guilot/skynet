from scanner_volumen.models import Direction, State
from scanner_volumen.strategy.entries import (
    FreezeTracker, es_entrada, score_suficiente,
)
from scanner_volumen.strategy.model import StrategyParams, TransitionRow

HORA = 3_600_000


def tr(prev, new, score=75.0):
    return TransitionRow(ts=0, symbol="A", prev_state=prev, new_state=new,
                         price=100.0, direction=Direction.LONG, score=score)


def test_es_entrada_solo_al_cruzar_hacia_watch_o_mas():
    assert es_entrada(tr(State.NORMAL, State.WATCH))
    assert es_entrada(tr(State.NORMAL, State.HOT))
    assert not es_entrada(tr(State.WATCH, State.HOT))    # ya estaba dentro
    assert not es_entrada(tr(State.HOT, State.NORMAL))   # sale, no entra


def test_score_suficiente_compara_con_el_umbral():
    params = StrategyParams(min_score_entrada=70.0)
    assert score_suficiente(tr(State.NORMAL, State.WATCH, score=70.0), params)
    assert not score_suficiente(tr(State.NORMAL, State.WATCH, score=69.9), params)


def test_freeze_congela_tras_tres_perdidas_en_la_ventana():
    f = FreezeTracker(StrategyParams())  # 3 pérdidas / 1h -> congela 3h
    for i in range(3):
        f.registrar("A", close_ts=i * 10 * 60_000, pnl=-1.0)
    ultimo_cierre = 2 * 10 * 60_000
    assert f.congelado("A", ultimo_cierre + HORA)
    assert not f.congelado("A", ultimo_cierre + 3 * HORA + 1)
    assert not f.congelado("B", ultimo_cierre)  # otro par no se ve afectado


def test_una_ganancia_rompe_la_racha():
    f = FreezeTracker(StrategyParams())
    f.registrar("A", close_ts=0, pnl=-1.0)
    f.registrar("A", close_ts=60_000, pnl=+1.0)
    f.registrar("A", close_ts=120_000, pnl=-1.0)
    f.registrar("A", close_ts=180_000, pnl=-1.0)
    assert not f.congelado("A", 200_000)


def test_un_pnl_de_cero_rompe_la_racha():
    # la condición de pérdida es estricta (pnl < 0): un pnl == 0 no es una
    # pérdida y rompe la racha igual que una ganancia, aunque no reste nada
    f = FreezeTracker(StrategyParams())
    f.registrar("A", close_ts=0, pnl=-1.0)
    f.registrar("A", close_ts=60_000, pnl=-1.0)
    f.registrar("A", close_ts=120_000, pnl=0.0)
    f.registrar("A", close_ts=180_000, pnl=-1.0)
    assert not f.congelado("A", 200_000)


def test_perdidas_fuera_de_la_ventana_no_congelan():
    f = FreezeTracker(StrategyParams())
    for i in range(3):
        f.registrar("A", close_ts=i * 2 * HORA, pnl=-1.0)  # separadas 2h
    assert not f.congelado("A", 4 * HORA + 1)


def test_freeze_desactivado_nunca_congela():
    f = FreezeTracker(StrategyParams(freeze_perdidas=0))
    for i in range(10):
        f.registrar("A", close_ts=i * 60_000, pnl=-1.0)
    assert not f.congelado("A", 10 * 60_000)
