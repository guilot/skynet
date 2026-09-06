import sqlite3

import pytest

from scanner_volumen.storage.db import open_readonly
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import CandleRepo
from scanner_volumen.models import Candle


def test_open_readonly_lee_datos_ya_existentes(tmp_path):
    ruta = tmp_path / "scanner.db"
    conn_escritura = open_db(ruta)
    CandleRepo(conn_escritura).save_many(
        "AAAUSDT", [Candle(ts=0, open=1, high=1, low=1, close=1, base_vol=1, quote_vol=1)]
    )
    conn_escritura.close()

    conn_ro = open_readonly(ruta)
    filas = conn_ro.execute("SELECT * FROM candles_1m").fetchall()
    assert len(filas) == 1
    conn_ro.close()


def test_open_readonly_no_permite_escribir(tmp_path):
    ruta = tmp_path / "scanner.db"
    open_db(ruta).close()

    conn_ro = open_readonly(ruta)
    with pytest.raises(sqlite3.OperationalError):
        conn_ro.execute("INSERT INTO candles_1m (symbol, ts, open, high, low, close, base_vol, quote_vol) "
                         "VALUES ('X', 0, 1, 1, 1, 1, 1, 1)")
    conn_ro.close()


def test_open_readonly_falla_con_mensaje_claro_si_no_existe(tmp_path):
    with pytest.raises(FileNotFoundError):
        open_readonly(tmp_path / "no_existe.db")
