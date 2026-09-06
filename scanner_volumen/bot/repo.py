"""Persistencia del bot de ejecución.

Guarda posiciones y sus salidas parciales, y deriva el equity en vez de
guardarlo: `equity_inicial + Σ pnl de las cerradas`. Así el saldo no puede
desincronizarse con los trades, y el capital se comporta como un saldo vivo —
si el bot se detiene con 850 USDT, al arrancar vuelve a operar sobre 850.

Todo se filtra por `modo` ('paper' / 'real') para que el dinero ficticio y el
real nunca se mezclen en el mismo informe.
"""
from __future__ import annotations

import sqlite3

from scanner_volumen.models import Direction
from scanner_volumen.strategy.model import ExitReason

_CLAVE_EQUITY_INICIAL = "equity_inicial"


class BotRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # --- posiciones ---

    def abrir(
        self, *, modo: str, symbol: str, direction: Direction, entry_ts: int,
        entry_price: float, entry_price_senal: float, margin: float,
        notional: float, size: float, fee_entrada: float,
    ) -> int:
        """`fee_entrada` se guarda aparte porque la reconstrucción tras un
        reinicio (Task 7) necesita recomponer el PnL de una posición todavía
        abierta, y la comisión de entrada ya se pagó. Derivarla de los
        parámetros funcionaría en paper, donde es determinista, pero no en la
        Fase 3, donde la cobra el exchange."""
        cur = self._conn.execute(
            "INSERT INTO bot_posiciones (modo, symbol, direction, entry_ts, "
            "entry_price, entry_price_senal, margin, notional, size, "
            "fee_entrada, abierta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (modo, symbol, direction.value, entry_ts, entry_price,
             entry_price_senal, margin, notional, size, fee_entrada),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def cerrar(
        self, posicion_id: int, *, close_ts: int, pnl: float, fees: float,
        max_rank: int,
    ) -> None:
        self._conn.execute(
            "UPDATE bot_posiciones SET abierta = 0, close_ts = ?, pnl = ?, "
            "fees = ?, max_rank = ? WHERE id = ?",
            (close_ts, pnl, fees, max_rank, posicion_id),
        )
        self._conn.commit()

    def abiertas(self, modo: str) -> list[dict]:
        filas = self._conn.execute(
            "SELECT * FROM bot_posiciones WHERE modo = ? AND abierta = 1 "
            "ORDER BY entry_ts, id",
            (modo,),
        ).fetchall()
        return [dict(f) for f in filas]

    def cerradas(self, modo: str, limite: int | None = None) -> list[dict]:
        """Sin `limite`: todas las cerradas, ascendente por `close_ts`
        -el orden que necesita `construir_resumen` para reconstruir el
        informe y que no se debe alterar-. Con `limite`, en cambio, se pide
        la página que le interesa a un consumidor tipo dashboard (las N más
        recientes): ordena DESCENDENTE y aplica `LIMIT` en la propia
        consulta SQL, para no traer la tabla entera a Python solo para
        recortarla después -algo que, con la operativa creciendo sin techo
        en la Fase 3, dejaría de ser gratis-."""
        if limite is None:
            filas = self._conn.execute(
                "SELECT * FROM bot_posiciones WHERE modo = ? AND abierta = 0 "
                "ORDER BY close_ts, id",
                (modo,),
            ).fetchall()
        else:
            filas = self._conn.execute(
                "SELECT * FROM bot_posiciones WHERE modo = ? AND abierta = 0 "
                "ORDER BY close_ts DESC, id DESC LIMIT ?",
                (modo, limite),
            ).fetchall()
        return [dict(f) for f in filas]

    # --- fills ---

    def registrar_fill(
        self, posicion_id: int, *, ts: int, reason: ExitReason, fraction: float,
        precio_referencia: float, precio: float, comision: float,
        tardio: bool = False,
    ) -> None:
        self._conn.execute(
            "INSERT INTO bot_fills (posicion_id, ts, reason, fraction, "
            "precio_referencia, precio, comision, tardio) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (posicion_id, ts, reason.value, fraction, precio_referencia,
             precio, comision, 1 if tardio else 0),
        )
        self._conn.commit()

    def fills_de(self, posicion_id: int) -> list[dict]:
        filas = self._conn.execute(
            "SELECT * FROM bot_fills WHERE posicion_id = ? ORDER BY ts, id",
            (posicion_id,),
        ).fetchall()
        return [dict(f) for f in filas]

    # --- equity ---

    def set_equity_inicial(self, valor: float) -> None:
        self._conn.execute(
            "INSERT INTO bot_meta (clave, valor) VALUES (?, ?) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
            (_CLAVE_EQUITY_INICIAL, repr(float(valor))),
        )
        self._conn.commit()

    def equity_inicial(self, defecto: float) -> float:
        fila = self._conn.execute(
            "SELECT valor FROM bot_meta WHERE clave = ?",
            (_CLAVE_EQUITY_INICIAL,),
        ).fetchone()
        return defecto if fila is None else float(fila["valor"])

    def equity(self, modo: str) -> float:
        """Saldo actual: el inicial más el PnL de lo ya cerrado.

        Las posiciones abiertas NO se descuentan: el backtest calcula el margen
        sobre un balance que solo se mueve al cerrar un trade, y replicarlo es
        obligatorio para que las dos corridas sean comparables."""
        fila = self._conn.execute(
            "SELECT COALESCE(SUM(pnl), 0.0) AS total FROM bot_posiciones "
            "WHERE modo = ? AND abierta = 0",
            (modo,),
        ).fetchone()
        return self.equity_inicial(defecto=0.0) + float(fila["total"])
