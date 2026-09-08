import sqlite3

import pytest

from scanner_volumen.bot.__main__ import main
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.models import Direction
from scanner_volumen.storage.db import open_db
from scanner_volumen.strategy.model import ExitReason

MIN = 60_000


def test_main_publica_los_contadores_persistidos_y_la_ventana(tmp_path, capsys):
    """A: el CLI debe leer descartes/transiciones/concurrencia máxima y el
    arranque persistidos, y pasárselos al informe -antes de este arreglo,
    ninguno se leía y el informe imprimía siempre "0"."""
    db = tmp_path / "scanner.db"
    conn = open_db(db)
    repo = BotRepo(conn)
    repo.set_equity_inicial("paper", 1000.0)
    repo.set_arrancado_ms(0)
    repo.incrementar_contador("paper", "NEUTRAL", 4)
    repo.incrementar_contador("paper", "transiciones", 37)
    repo.fijar_maximo("paper", "max_concurrentes", 3)

    pid = repo.abrir(modo="paper", symbol="AAAUSDT", direction=Direction.LONG,
                     entry_ts=MIN, entry_price=100.0, entry_price_senal=100.0,
                     margin=20.0, notional=400.0, size=4.0, fee_entrada=0.0)
    repo.registrar_fill(pid, ts=2 * MIN, reason=ExitReason.STOP, fraction=1.0,
                        precio_referencia=97.5, precio=97.0, comision=0.0,
                        precio_regla=97.5)
    repo.cerrar(pid, close_ts=2 * MIN, pnl=-12.0, fees=0.0, max_rank=1)
    conn.close()

    main(["--db", str(db)])
    salida = capsys.readouterr().out

    assert "  descartes NEUTRAL:          4" in salida
    assert "37 transiciones" in salida
    assert "Concurrencia máxima: 3" in salida
    # arrancado_ms=0 debe mandar sobre el ts del primer trade (MIN): sin
    # esto, la "Ventana" saldría corta si el bot llevara tiempo sin operar.
    assert "1970-01-01 00:00" in salida
    assert "STOP" in salida
    assert "+51.3 bps" in salida  # (97.5 - 97.0) / 97.5 * 10000


def test_main_publica_el_saldo_real_persistido_en_modo_real(tmp_path, capsys):
    """Task 11: el CLI (solo lectura, sin red) debe leer el saldo real que
    el proceso en vivo dejo persistido con `BotRepo.set_saldo_real` y
    pasarselo al informe -sin esto, el bloque de modo real siempre diria
    "sin dato todavia", incluso con el bot corriendo de verdad."""
    db = tmp_path / "scanner.db"
    conn = open_db(db)
    repo = BotRepo(conn)
    repo.set_equity_inicial("real", 1000.0)
    repo.set_saldo_real("real", 995.0)
    conn.close()

    main(["--db", str(db), "--modo", "real"])
    salida = capsys.readouterr().out
    assert "Saldo real: 995.00" in salida


def test_main_no_revienta_contra_una_base_sin_bot_contadores(tmp_path, capsys):
    """`open_readonly` (el que usa este CLI) nunca migra la base que abre
    -no debe arriesgarse a escribir en una base que puede estar en uso-, así
    que una base creada antes de esta migración no tiene `bot_contadores`.
    El informe debe seguir imprimiéndose (contadores en cero), no reventar
    con `OperationalError: no such table`."""
    db = tmp_path / "vieja.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE bot_posiciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT, modo TEXT NOT NULL,
            symbol TEXT NOT NULL, direction TEXT NOT NULL,
            entry_ts INTEGER NOT NULL, entry_price REAL NOT NULL,
            entry_price_senal REAL NOT NULL, margin REAL NOT NULL,
            notional REAL NOT NULL, size REAL NOT NULL,
            fee_entrada REAL NOT NULL, abierta INTEGER NOT NULL,
            close_ts INTEGER, pnl REAL, fees REAL, max_rank INTEGER
        );
        CREATE TABLE bot_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT, posicion_id INTEGER NOT NULL,
            ts INTEGER NOT NULL, reason TEXT NOT NULL, fraction REAL NOT NULL,
            precio_referencia REAL NOT NULL, precio REAL NOT NULL,
            comision REAL NOT NULL, tardio INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE bot_meta (clave TEXT PRIMARY KEY, valor TEXT NOT NULL);
        """
    )
    conn.commit()
    conn.close()

    main(["--db", str(db)])  # no debe lanzar
    salida = capsys.readouterr().out
    assert "Trades ejecutados: 0" in salida
    assert "descartes NEUTRAL:          0" in salida
