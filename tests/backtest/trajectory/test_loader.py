import pytest

from scanner_volumen.backtest.trajectory.loader import (
    load_transitions, make_candle_provider,
)
from scanner_volumen.models import Candle, Direction, State
from scanner_volumen.scoring.states import Transition
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import CandleRepo, StateTransitionRepo


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "t.db")
    yield c
    c.close()


def test_load_transitions_mapea_a_enums(conn):
    repo = StateTransitionRepo(conn)
    repo.insert(
        Transition(symbol="BTCUSDT", previous=State.NORMAL, current=State.WATCH,
                   score=55.0, escalated=True, ts=1000, should_alert=False),
        price=10.0, direction=Direction.LONG,
        config_fingerprint="c" * 64, code_revision="rev",
    )
    filas = load_transitions(repo)
    assert len(filas) == 1
    assert filas[0].prev_state is State.NORMAL
    assert filas[0].new_state is State.WATCH
    assert filas[0].direction is Direction.LONG
    assert filas[0].price == 10.0


def test_candle_provider_devuelve_candlerows(conn):
    CandleRepo(conn).save_many("BTCUSDT", [
        Candle(ts=1000, open=1, high=2, low=0.5, close=1.5,
               base_vol=10, quote_vol=15),
    ])
    provider = make_candle_provider(CandleRepo(conn))
    velas = provider("BTCUSDT", 0)
    assert velas[0].high == 2 and velas[0].low == 0.5 and velas[0].close == 1.5
