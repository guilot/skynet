import pytest

from scanner_volumen.backtest.trajectory.portfolio import run_trajectory, settle
from scanner_volumen.models import Direction, State
from scanner_volumen.strategy.model import (
    CandleRow, ExitReason, Fill, PositionOutcome, StrategyParams, TransitionRow,
)

MIN0 = 60_000


def test_settle_long_sin_comision():
    # entra a 100, cierra todo a 110 => +10% de precio; margen 20, 20x
    # nocional=400, tamaño=4 unidades; pnl bruto = (110-100)*4 = 40
    out = PositionOutcome(
        symbol="X", direction=Direction.LONG, entry_ts=0, entry_price=100.0,
        fills=(Fill(ts=MIN0, price=110.0, fraction=1.0, reason=ExitReason.EXTREME),),
        max_rank=4, close_ts=MIN0,
    )
    params = StrategyParams(comision_taker=0.0)
    trade = settle(out, margin=20.0, params=params)
    assert trade.notional == pytest.approx(400.0)
    assert trade.size == pytest.approx(4.0)
    assert trade.pnl == pytest.approx(40.0)
    assert trade.fees == pytest.approx(0.0)


def test_settle_descuenta_comision_entrada_y_salida():
    out = PositionOutcome(
        symbol="X", direction=Direction.LONG, entry_ts=0, entry_price=100.0,
        fills=(Fill(ts=MIN0, price=100.0, fraction=1.0, reason=ExitReason.STALE_BE),),
        max_rank=1, close_ts=MIN0,
    )
    params = StrategyParams(comision_taker=0.0006)
    trade = settle(out, margin=20.0, params=params)
    # nocional entrada=400, salida=400 => comisión = 0.0006*400*2 = 0.48
    assert trade.fees == pytest.approx(0.48)
    assert trade.pnl == pytest.approx(-0.48)


MIN = 60_000


def _tr(ts, prev, new, price, symbol="A", direction=Direction.LONG, score=75.0):
    return TransitionRow(ts=ts, symbol=symbol, prev_state=prev, new_state=new,
                         price=price, direction=direction, score=score)


def test_descarta_neutral_y_respeta_unicidad_por_simbolo():
    trans = [
        _tr(0, State.NORMAL, State.WATCH, 100.0, direction=Direction.NEUTRAL),
        _tr(1 * MIN, State.NORMAL, State.WATCH, 100.0, symbol="B"),
        _tr(2 * MIN, State.WATCH, State.NORMAL, 100.0, symbol="B"),
        # segunda entrada de B mientras la primera sigue abierta -> descartada
        _tr(3 * MIN, State.NORMAL, State.WATCH, 100.0, symbol="B"),
    ]

    def candles_for(symbol, since):
        return [CandleRow(ts=since + i * MIN, open=100, high=100, low=100, close=100)
                for i in range(40)]

    run = run_trajectory(trans, candles_for, StrategyParams(comision_taker=0.0))
    assert run.skipped_neutral == 1
    assert run.skipped_symbol_open == 1
    assert len(run.trades) == 1  # solo la primera entrada de B


def test_filtro_min_score_entrada():
    # dos entradas: una con score 52 (< umbral) y otra con 60 (>= umbral).
    trans = [
        _tr(0, State.NORMAL, State.WATCH, 100.0, symbol="A", score=52.0),
        _tr(1 * MIN, State.NORMAL, State.WATCH, 100.0, symbol="B", score=60.0),
    ]

    def candles_for(symbol, since):
        return [CandleRow(ts=since + i * MIN, open=100, high=100, low=100, close=100)
                for i in range(40)]

    run = run_trajectory(trans, candles_for,
                         StrategyParams(comision_taker=0.0, min_score_entrada=55.0))
    assert run.skipped_score_bajo == 1
    assert len(run.trades) == 1
    assert run.trades[0].outcome.symbol == "B"


def test_congela_par_tras_3_perdidas_en_1h():
    # 4 entradas LONG del mismo par que paran en perdida; las 3 primeras caen
    # en <1h -> la 4ª (dentro de las 3h siguientes) queda congelada.
    trans = [
        _tr(0, State.NORMAL, State.WATCH, 100.0, symbol="A"),
        _tr(10 * MIN, State.NORMAL, State.WATCH, 100.0, symbol="A"),
        _tr(20 * MIN, State.NORMAL, State.WATCH, 100.0, symbol="A"),
        _tr(30 * MIN, State.NORMAL, State.WATCH, 100.0, symbol="A"),
    ]

    def candles_for(symbol, since):
        # entra a 100 y cae a 97 (bajo el stop 97.5) -> perdida al minuto
        precios = [100.0] + [97.0] * 12
        return [CandleRow(ts=since + i * MIN, open=p, high=p, low=p, close=p)
                for i, p in enumerate(precios)]

    run = run_trajectory(trans, candles_for, StrategyParams(comision_taker=0.0))
    assert len(run.trades) == 3
    assert all(t.pnl < 0 for t in run.trades)
    assert run.skipped_congelado == 1


def test_freeze_desactivado_no_congela():
    trans = [
        _tr(i * 10 * MIN, State.NORMAL, State.WATCH, 100.0, symbol="A")
        for i in range(4)
    ]

    def candles_for(symbol, since):
        precios = [100.0] + [97.0] * 12
        return [CandleRow(ts=since + i * MIN, open=p, high=p, low=p, close=p)
                for i, p in enumerate(precios)]

    run = run_trajectory(trans, candles_for,
                         StrategyParams(comision_taker=0.0, freeze_perdidas=0))
    assert len(run.trades) == 4
    assert run.skipped_congelado == 0


def test_limite_de_cinco_concurrentes():
    # 6 símbolos entran a la vez y ninguno cierra pronto (precios planos)
    trans = [
        _tr(0, State.NORMAL, State.WATCH, 100.0, symbol=s)
        for s in ("A", "B", "C", "D", "E", "F")
    ]

    def candles_for(symbol, since):
        return [CandleRow(ts=since + i * MIN, open=100, high=100, low=100, close=100)
                for i in range(40)]

    run = run_trajectory(trans, candles_for, StrategyParams(comision_taker=0.0))
    assert run.skipped_max_concurrent == 1
    assert len(run.trades) == 5
    assert run.max_concurrentes_alcanzado == 5  # tope alcanzado, el 6º se descarta


def test_descarta_entrada_sin_cobertura_de_velas():
    # A tiene velas desde antes de su entrada -> simula normalmente.
    # B entra en ts=0 pero sus velas (tras la poda de retención) solo
    # empiezan mucho más tarde -> se descarta sin ocupar slot ni simular.
    trans = [
        _tr(0, State.NORMAL, State.WATCH, 100.0, symbol="A"),
        _tr(0, State.NORMAL, State.WATCH, 100.0, symbol="B"),
    ]

    def candles_for(symbol, since):
        if symbol == "B":
            # las velas de B solo cubren mucho después de su entrada (podadas)
            inicio = since + 1000 * MIN
        else:
            inicio = since
        return [CandleRow(ts=inicio + i * MIN, open=100, high=100, low=100, close=100)
                for i in range(40)]

    run = run_trajectory(trans, candles_for, StrategyParams(comision_taker=0.0))
    assert run.skipped_sin_velas == 1
    assert len(run.trades) == 1
    assert run.trades[0].outcome.symbol == "A"


def test_margen_usa_balance_compuesto_tras_cierre_previo():
    # A entra en ts=0 y salta directo a EXTREME en ts=1*MIN (dispara los tramos
    # HOT/SIGNAL/EXTREME de una vez y cierra el 100% con ganancia conocida).
    # B entra en ts=2*MIN, estrictamente después de que A ya cerró: su margen
    # debe calcularse sobre el balance ya compuesto con el pnl de A, no sobre
    # equity_inicial fijo.
    trans = [
        _tr(0, State.NORMAL, State.WATCH, 100.0, symbol="A"),
        _tr(1 * MIN, State.WATCH, State.EXTREME, 110.0, symbol="A"),
        _tr(2 * MIN, State.NORMAL, State.WATCH, 100.0, symbol="B"),
    ]

    def candles_for(symbol, since):
        n = 5 if symbol == "A" else 40
        return [CandleRow(ts=since + i * MIN, open=100, high=100, low=100, close=100)
                for i in range(n)]

    params = StrategyParams(comision_taker=0.0)
    run = run_trajectory(trans, candles_for, params)

    trade_a = next(t for t in run.trades if t.outcome.symbol == "A")
    trade_b = next(t for t in run.trades if t.outcome.symbol == "B")

    assert trade_a.pnl > 0
    assert trade_a.outcome.close_ts <= 2 * MIN  # A cierra antes de que B entre
    assert trade_b.margin == pytest.approx(
        params.fraccion_margen * (params.equity_inicial + trade_a.pnl)
    )
