import pytest

from scanner_volumen.backtest.trajectory.__main__ import main
from scanner_volumen.models import Candle, Direction, State
from scanner_volumen.scoring.states import Transition
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import CandleRepo, StateTransitionRepo

MIN = 60_000


def test_main_corre_end_to_end(tmp_path, capsys):
    db = tmp_path / "scanner.db"
    conn = open_db(db)
    st = StateTransitionRepo(conn)
    cr = CandleRepo(conn)
    # una entrada que escala a EXTREME
    for ts, prev, new, price in [
        (0, State.NORMAL, State.WATCH, 100.0),
        (1 * MIN, State.WATCH, State.EXTREME, 130.0),
    ]:
        st.insert(
            Transition(symbol="BTCUSDT", previous=prev, current=new, score=95.0,
                       escalated=True, ts=ts, should_alert=False),
            price=price, direction=Direction.LONG,
            config_fingerprint="c" * 64, code_revision="rev",
        )
    cr.save_many("BTCUSDT", [
        Candle(ts=i * MIN, open=100 + i, high=100 + i, low=100 + i, close=100 + i,
               base_vol=1, quote_vol=1)
        for i in range(5)
    ])
    conn.close()

    main(["--db", str(db), "--fee", "0"])
    salida = capsys.readouterr().out
    assert "Backtest de trayectoria" in salida
    assert "Equity:" in salida
