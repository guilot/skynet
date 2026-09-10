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

_CLAVE_ARRANCADO_MS = "arrancado_ms"


def _clave_equity_inicial(modo: str) -> str:
    """`equity_inicial` segmentada por modo: sin esto, el día que exista
    operativa `real` arrancaría sobre el capital del `paper` (o viceversa),
    porque las dos comparten la misma fila de `bot_meta`."""
    return f"equity_inicial:{modo}"


def _clave_saldo_real(modo: str) -> str:
    """El último saldo real conocido (Task 11), segmentado por modo igual
    que `equity_inicial`: en `paper` esta clave nunca se escribe, así que
    leerla sin haberla fijado nunca debe devolver `None`, no un dato de
    `real` filtrado por error."""
    return f"saldo_real:{modo}"


def _clave_saldo_dia(modo: str, dia: str) -> str:
    """El saldo de referencia del freno de pérdida diaria (`bot/frenos.py`),
    segmentado por modo -igual que `equity_inicial`- y por día (`dia` en
    formato `AAAA-MM-DD`, siempre UTC): sin el día en la clave, la referencia
    de ayer seguiría vigente hoy y el freno nunca se liberaría."""
    return f"saldo_dia:{modo}:{dia}"


class BotRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # --- posiciones ---

    def abrir(
        self, *, modo: str, symbol: str, direction: Direction, entry_ts: int,
        entry_price: float, entry_price_senal: float, margin: float,
        notional: float, size: float, fee_entrada: float,
        client_oid: str | None = None, confirmada: bool = True,
    ) -> int:
        """`fee_entrada` se guarda aparte porque la reconstrucción tras un
        reinicio (Task 7) necesita recomponer el PnL de una posición todavía
        abierta, y la comisión de entrada ya se pagó. Derivarla de los
        parámetros funcionaría en paper, donde es determinista, pero no en la
        Fase 3, donde la cobra el exchange.

        `client_oid` identifica la orden ante el exchange: es lo que permite
        a la reconciliación (Task 8) reconocer como propia una posición que
        el bot mandó pero de la que un proceso muerto a medias no llegó a
        registrar el resultado (ver `por_client_oid`).

        `confirmada = 0` significa "orden mandada, resultado desconocido":
        es exactamente la huella que deja ese proceso muerto a medias.
        `BotRunner._abrir` reserva la fila con `confirmada=False` -y
        `entry_price`/`size` provisionales, porque ambas columnas son `NOT
        NULL`- ANTES de mandar la orden, y la cierra con
        `confirmar_apertura` al recibir la respuesta del broker. El defecto
        `True` mantiene el comportamiento de los llamadores (paper, tests)
        que ya conocen el resultado real de la orden en el momento de
        escribir la fila."""
        cur = self._conn.execute(
            "INSERT INTO bot_posiciones (modo, symbol, direction, entry_ts, "
            "entry_price, entry_price_senal, margin, notional, size, "
            "fee_entrada, client_oid, confirmada, abierta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (modo, symbol, direction.value, entry_ts, entry_price,
             entry_price_senal, margin, notional, size, fee_entrada,
             client_oid, 1 if confirmada else 0),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def confirmar_apertura(
        self, posicion_id: int, *, entry_price: float, size: float,
        fee_entrada: float,
    ) -> None:
        """Cierra la reserva de `abrir(..., confirmada=False)` con el
        resultado real de la orden: sobrescribe el precio de entrada y el
        tamaño provisionales (los de la señal) con los que devolvió el
        broker, y marca `confirmada = 1`."""
        self._conn.execute(
            "UPDATE bot_posiciones SET entry_price = ?, size = ?, "
            "fee_entrada = ?, confirmada = 1 WHERE id = ?",
            (entry_price, size, fee_entrada, posicion_id),
        )
        self._conn.commit()

    def por_client_oid(self, modo: str, client_oid: str) -> dict | None:
        """La fila reservada con este `client_oid`, dentro de `modo`, o
        `None` si no existe. Task 8 la usa para reconocer como propia una
        posición que aparece en el exchange y no en `abiertas()` -el caso
        exacto de un proceso que murió tras mandar la orden pero antes de
        confirmar la fila."""
        fila = self._conn.execute(
            "SELECT * FROM bot_posiciones WHERE modo = ? AND client_oid = ?",
            (modo, client_oid),
        ).fetchone()
        return dict(fila) if fila is not None else None

    def reservadas_sin_confirmar(self, modo: str) -> list[dict]:
        """Filas con `confirmada = 0`: la huella de un proceso que murió
        entre mandar la orden y registrar su resultado. Task 8 las usa en la
        reconciliación de arranque para decidir si la orden llegó a
        ejecutarse en el exchange (y entonces hay que completarlas) o no (y
        entonces se descartan)."""
        filas = self._conn.execute(
            "SELECT * FROM bot_posiciones WHERE modo = ? AND confirmada = 0 "
            "ORDER BY entry_ts, id",
            (modo,),
        ).fetchall()
        return [dict(f) for f in filas]

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

    def marcar_degradada(self, posicion_id: int) -> None:
        """El runner la llama cuando aísla una posición tras un fallo del
        broker. Sin persistir el flag, una posición degradada sigue
        `abierta = 1` para siempre y desaparece del informe sin dejar
        rastro -sistemáticamente las que iban perdiendo, porque el camino
        más probable a degradarse es un fallo al ejecutar un STOP."""
        self._conn.execute(
            "UPDATE bot_posiciones SET degradada = 1 WHERE id = ?", (posicion_id,)
        )
        self._conn.commit()

    def fijar_stop_id(self, posicion_id: int, stop_id: str | None) -> None:
        """Persiste el `stop_id` del stop vigente en el exchange para esta
        posición: al colocarlo por primera vez y cada vez que se mueve (un
        `stop_id` nuevo, porque mover un stop es cancelar el viejo y colocar
        otro). Sin esto, un reinicio del bot perdería el identificador y no
        podría ni moverlo ni cancelarlo nunca más -la garantía central de
        esta fase (el stop sobrevive a una caída del proceso) no sirve de
        nada si el proceso, al volver, no sabe cuál es."""
        self._conn.execute(
            "UPDATE bot_posiciones SET stop_id = ? WHERE id = ?",
            (stop_id, posicion_id),
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
        tardio: bool = False, precio_regla: float | None = None,
        cierre_exchange: bool = False,
    ) -> None:
        """`precio_regla` es el nivel que la regla prometía para esta salida
        (el stop vigente, el break-even, o el precio de la transición, según
        el motivo); `None` cuando no hay nivel prometido contra el que medir
        (p. ej. `EXTREME` por temporizador, que cierra a mercado adrede). Lo
        decide el runner, no este repositorio: aquí solo se persiste.

        `cierre_exchange` distingue un cierre que decidió el exchange por su
        cuenta -el stop saltó, o hubo liquidación, mientras el bot miraba a
        otro lado (Task 9, sondeo periódico)- de uno que decidió el motor de
        reglas en caliente. No se traduce a un `ExitReason` nuevo a
        propósito: en espíritu sigue siendo la regla del stop ejecutándose
        (`reason=STOP`), y `ExitReason` es el enumerado que el informe
        compartido con el backtest recorre entero para el bloque de "PnL por
        motivo de salida" -un valor nuevo metería ahí una línea nueva y
        rompería su golden master sin que el backtest hubiera cambiado."""
        self._conn.execute(
            "INSERT INTO bot_fills (posicion_id, ts, reason, fraction, "
            "precio_referencia, precio, comision, tardio, precio_regla, "
            "cierre_exchange) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (posicion_id, ts, reason.value, fraction, precio_referencia,
             precio, comision, 1 if tardio else 0, precio_regla,
             1 if cierre_exchange else 0),
        )
        self._conn.commit()

    def posicion(self, posicion_id: int) -> dict | None:
        """Una posición por su id, o `None` si no existe.

        La usa el panel para el desglose de un trade. Devuelve la fila
        entera -incluido `modo`- porque quien llama tiene que poder
        comprobar que la posición pertenece al modo que está mirando: los
        libros de `paper` y `real` son distintos y no deben mezclarse en la
        misma vista."""
        fila = self._conn.execute(
            "SELECT * FROM bot_posiciones WHERE id = ?", (posicion_id,),
        ).fetchone()
        return dict(fila) if fila is not None else None

    def fills_de(self, posicion_id: int) -> list[dict]:
        filas = self._conn.execute(
            "SELECT * FROM bot_fills WHERE posicion_id = ? ORDER BY ts, id",
            (posicion_id,),
        ).fetchall()
        return [dict(f) for f in filas]

    # --- equity ---

    def set_equity_inicial(self, modo: str, valor: float) -> None:
        self._conn.execute(
            "INSERT INTO bot_meta (clave, valor) VALUES (?, ?) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
            (_clave_equity_inicial(modo), repr(float(valor))),
        )
        self._conn.commit()

    def equity_inicial(self, modo: str, defecto: float) -> float:
        fila = self._conn.execute(
            "SELECT valor FROM bot_meta WHERE clave = ?",
            (_clave_equity_inicial(modo),),
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
        return self.equity_inicial(modo, defecto=0.0) + float(fila["total"])

    def set_saldo_real(self, modo: str, valor: float) -> None:
        """Persiste el último saldo real conocido de la subcuenta (Task 11):
        `equity()` deriva su cifra de la propia base y no necesita red, pero
        el informe también quiere mostrar el saldo REAL -el de Bitget- para
        compararlo contra ese equity calculado (la diferencia mide funding,
        comisiones no modeladas y redondeos). El proceso en vivo es el único
        que tiene una conexión abierta al exchange; esto es lo que le permite
        al CLI de informe (solo lectura, sin red) enseñarlo igualmente -lee
        el último valor que el proceso en vivo dejó aquí.

        CONTRATO CON LA TASK 13: debe llamarse en cada tick en el que el modo
        efectivo sea real (con el `realizado` de `BitgetPrivate.get_saldo()`,
        el mismo valor que alimenta a `LivePortfolio` vía `proveedor_saldo`).
        En `paper` nunca se llama -no hay saldo real que persistir-, y por
        eso `format_bloque_ejecucion` no muestra este bloque en `paper`."""
        self._conn.execute(
            "INSERT INTO bot_meta (clave, valor) VALUES (?, ?) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
            (_clave_saldo_real(modo), repr(float(valor))),
        )
        self._conn.commit()

    def saldo_real(self, modo: str) -> float | None:
        """El último saldo real persistido por `set_saldo_real`, o `None` si
        todavía no se ha guardado ninguno -por ejemplo, un informe pedido
        antes de que el proceso en vivo complete su primer tick en real."""
        fila = self._conn.execute(
            "SELECT valor FROM bot_meta WHERE clave = ?",
            (_clave_saldo_real(modo),),
        ).fetchone()
        return None if fila is None else float(fila["valor"])

    # --- frenos (Task 10) ---

    def fijar_saldo_dia(self, modo: str, dia: str, valor: float) -> None:
        """Fija el saldo de referencia de `dia` (persistido en `bot_meta`,
        nunca en memoria): un reinicio bajo `Restart=always` en pleno
        frenazo tiene que encontrar exactamente el mismo valor con el que
        empezó el día, no recalcularlo sobre el saldo ya castigado."""
        self._conn.execute(
            "INSERT INTO bot_meta (clave, valor) VALUES (?, ?) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
            (_clave_saldo_dia(modo, dia), repr(float(valor))),
        )
        self._conn.commit()

    def saldo_dia(self, modo: str, dia: str) -> float | None:
        """El saldo de referencia ya fijado para `dia`, o `None` si todavía
        no se ha fijado ninguno -es la señal que usa `Frenos` para decidir
        si esta es la primera consulta del día."""
        fila = self._conn.execute(
            "SELECT valor FROM bot_meta WHERE clave = ?",
            (_clave_saldo_dia(modo, dia),),
        ).fetchone()
        return None if fila is None else float(fila["valor"])

    # --- arranque ---

    def set_arrancado_ms(self, valor: int) -> None:
        """Mismo patrón que `set_equity_inicial`: el llamador lee el valor ya
        guardado (o el `ahora` de este arranque como defecto) y lo vuelve a
        fijar, así que arranques posteriores respetan el original."""
        self._conn.execute(
            "INSERT INTO bot_meta (clave, valor) VALUES (?, ?) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
            (_CLAVE_ARRANCADO_MS, str(int(valor))),
        )
        self._conn.commit()

    def arrancado_ms(self, defecto: int | None = None) -> int | None:
        fila = self._conn.execute(
            "SELECT valor FROM bot_meta WHERE clave = ?",
            (_CLAVE_ARRANCADO_MS,),
        ).fetchone()
        return defecto if fila is None else int(fila["valor"])

    # --- contadores del informe ---

    def incrementar_contador(self, modo: str, clave: str, cantidad: int = 1) -> None:
        self._conn.execute(
            "INSERT INTO bot_contadores (modo, clave, valor) VALUES (?, ?, ?) "
            "ON CONFLICT(modo, clave) DO UPDATE SET valor = valor + excluded.valor",
            (modo, clave, cantidad),
        )
        self._conn.commit()

    def fijar_maximo(self, modo: str, clave: str, valor: int) -> None:
        """Solo sube: si lo ya guardado es mayor, se queda como está."""
        self._conn.execute(
            "INSERT INTO bot_contadores (modo, clave, valor) VALUES (?, ?, ?) "
            "ON CONFLICT(modo, clave) DO UPDATE SET "
            "valor = MAX(valor, excluded.valor)",
            (modo, clave, valor),
        )
        self._conn.commit()

    def contadores(self, modo: str) -> dict[str, int]:
        """Los contadores persistidos de `modo`.

        Devuelve vacío -no revienta- si `bot_contadores` todavía no existe:
        `open_readonly` (el CLI del informe, ver `bot/__main__.py`) nunca
        migra la base que abre a propósito -no debe arriesgarse a escribir
        en una base que puede estar en uso-, así que leer contadores contra
        una base anterior a esta migración debe comportarse como "nada
        contado todavía", no fallar con `OperationalError`."""
        try:
            filas = self._conn.execute(
                "SELECT clave, valor FROM bot_contadores WHERE modo = ?", (modo,)
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        return {f["clave"]: f["valor"] for f in filas}
