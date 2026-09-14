"""Regenera `state_transitions` a partir de velas historicas, reproduciendo
el motor de puntuacion minuto a minuto.

    .venv/bin/python herramientas/reconstruir_transiciones.py \
        --velas data/historico.db --salida data/historico.db

Con eso el backtest de trayectoria puede correr sobre meses de historia en
vez de las dos semanas que el escaner lleva recopilando.

## Que se reproduce fielmente y que no

De las 13 entradas del score, DOCE salen solo de las velas (los seis
retornos, los tres volumenes relativos, el estallido de demanda, el VWAP y el
z-score). Solo `market_cap` no es reconstruible: se aproxima con el
suministro de HOY multiplicado por el precio historico.

`ret_24h` merece mencion aparte porque casi se cuela: SI puntua, y en vivo
viene del TICKER, no de las velas. Pasar `ticker=None` habria anulado esa
dimension del momento EN SILENCIO -sin error, solo un componente valiendo
cero-. Aqui se sintetiza un `Ticker` con `change_24h` calculado desde el
propio buffer.

`open_interest` y `funding_rate` no puntuan (se comprobo contra
CLAVES_MOMENTUM/DEMAND/STRUCTURE), asi que su ausencia no afecta al score.

## La limitacion que NO se puede arreglar: la cadencia

El escaner en vivo evalua cada `tick_seconds` (1 segundo) contra la vela EN
CURSO. Esta reconstruccion solo puede evaluar una vez por vela cerrada,
porque un minuto es la granularidad mas fina que da el historico de Bitget.

Medido contra las transiciones que el escaner genero en vivo sobre los mismos
datos: la reconstruccion produce el **76%** de ellas (CYSUSDT, un alt-coin
tipico de los que opera la estrategia), con un score medio de 46,6 frente a
47,5. Las que faltan son picos intra-minuto que el escaner ve y esta
reconstruccion no.

No se sabe si esas transiciones perdidas habrian dado mejores o peores
trades, asi que **no se puede afirmar en que direccion sesga**. Lo que si se
puede decir es que el backtest historico es una version MAS GRUESA de la
estrategia, no la misma.
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

from scanner_volumen.config import load_config
from scanner_volumen.engine.candles import CandleBuffer
from scanner_volumen.engine.metrics import MetricsBuilder
from scanner_volumen.engine.profile import build_profile
from scanner_volumen.models import Candle, Ticker
from scanner_volumen.scoring.score import score_symbol
from scanner_volumen.scoring.states import StateMachine

MIN_MS = 60_000
DIA_MS = 1440 * MIN_MS


def _velas_de(conn: sqlite3.Connection, symbol: str) -> list[Candle]:
    return [Candle(ts=r[0], open=r[1], high=r[2], low=r[3], close=r[4],
                   base_vol=r[5], quote_vol=r[6])
            for r in conn.execute(
                "SELECT ts,open,high,low,close,base_vol,quote_vol FROM candles_1m "
                "WHERE symbol=? ORDER BY ts", (symbol,))]


def _ticker_sintetico(symbol: str, buffer: CandleBuffer, ahora: int) -> Ticker | None:
    """`ret_24h` es la unica entrada del score que en vivo llega por ticker.

    Se reconstruye desde el buffer: la vela de hace 24h contra la actual. Si
    no hay 24h de historia todavia se devuelve `None`, que es honesto -en ese
    momento el dato no existe- y deja ese componente sin puntuar, igual que
    le pasa al escaner en un arranque en frio."""
    actual = buffer.current()
    if actual is None:
        return None
    hace_24h = None
    for c in buffer.closed(1500):
        if c.ts <= ahora - DIA_MS:
            hace_24h = c
    if hace_24h is None or hace_24h.close <= 0:
        return None
    cambio = 100.0 * (actual.close - hace_24h.close) / hace_24h.close
    return Ticker(symbol=symbol, last=actual.close, change_24h=cambio,
                  volume_24h_usdt=0.0, open_interest=0.0, funding_rate=0.0,
                  ts=ahora)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--velas", type=Path, default=Path("data/historico.db"))
    p.add_argument("--salida", type=Path, default=Path("data/historico.db"))
    p.add_argument("--config", type=Path, default=Path("config.toml"))
    p.add_argument("--dias-perfil", type=int, default=14,
                   help="dias de calentamiento antes de emitir transiciones")
    p.add_argument("--suministros", type=Path, default=Path(".backtest-data/scanner.db"),
                   help="base con la tabla `supply_cache` de la que sacar el "
                        "suministro circulante")
    args = p.parse_args()

    cfg = load_config(args.config)
    origen = sqlite3.connect(args.velas)
    destino = sqlite3.connect(args.salida)
    destino.execute("""CREATE TABLE IF NOT EXISTS state_transitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
        symbol TEXT NOT NULL, prev_state TEXT NOT NULL, new_state TEXT NOT NULL,
        score REAL NOT NULL, price REAL, direction TEXT, escalated INTEGER,
        config_fingerprint TEXT, code_revision TEXT)""")
    destino.execute("CREATE INDEX IF NOT EXISTS idx_st_sym_ts "
                    "ON state_transitions(symbol, ts)")
    destino.execute("DELETE FROM state_transitions")
    destino.commit()

    # `market_cap` es la UNICA de las 13 entradas del score que no se puede
    # reconstruir: se aproxima con el suministro de HOY por el precio de
    # entonces. Pasarlo como `None` -que era la primera version- no es
    # neutral: ese componente vale hasta 5 puntos y su ausencia bajaba el
    # score medio unos 4, generando bastantes menos transiciones que el
    # escaner en vivo sobre los mismos datos.
    suministros: dict[str, float] = {}
    try:
        sc = sqlite3.connect(args.suministros)
        suministros = {r[0]: r[1] for r in sc.execute(
            "SELECT symbol, circulating_supply FROM supply_cache "
            "WHERE circulating_supply IS NOT NULL")}
        sc.close()
    except sqlite3.Error as exc:
        print(f"  AVISO: sin suministros ({exc}); el componente market_cap "
              f"quedara sin puntuar y el score saldra ~4 puntos bajo")
    print(f"  suministros disponibles para {len(suministros)} simbolos")

    simbolos = [r[0] for r in origen.execute(
        "SELECT DISTINCT symbol FROM candles_1m ORDER BY symbol")]
    print(f"  {len(simbolos)} simbolos   calentamiento: {args.dias_perfil} dias\n")

    t0 = time.time()
    total = 0
    for i, sym in enumerate(simbolos, 1):
        velas = _velas_de(origen, sym)
        if len(velas) < args.dias_perfil * 1440:
            print(f"  [{i}/{len(simbolos)}] {sym:16} SALTADO "
                  f"({len(velas):,} velas, insuficientes)", flush=True)
            continue

        corte = velas[0].ts + args.dias_perfil * DIA_MS
        # El perfil de volumen se construye con el calentamiento y NO se
        # recalcula: en vivo se recalcula a diario (mantenimiento), asi que
        # esto es una simplificacion conocida. Recalcularlo por dia
        # multiplicaria el coste y es materia de una version posterior.
        perfil = build_profile(sym, [c for c in velas if c.ts < corte], cfg.profile)
        buffer = CandleBuffer(sym)
        for c in velas:
            if c.ts < corte:
                buffer.upsert(c)

        motor = MetricsBuilder(cfg.engine, cfg.profile)
        maquina = StateMachine(cfg.states)
        filas = []
        for c in velas:
            if c.ts < corte:
                continue
            buffer.upsert(c)
            ahora = c.ts + MIN_MS - 1        # la vela ya cerrada
            sumin = suministros.get(sym)
            cap = sumin * c.close if sumin else None
            m = motor.compute(sym, buffer, perfil,
                              _ticker_sintetico(sym, buffer, ahora), cap, ahora)
            d = score_symbol(m, cfg.score)
            tr = maquina.update(sym, d.total, ahora)
            if tr is None:
                continue
            if tr.previous.rank == 0 and tr.current.rank == 0:
                continue
            filas.append((tr.ts, sym, tr.previous.name, tr.current.name, d.total,
                          m.price, d.direction.value, int(tr.escalated),
                          "historico", "reconstruido"))
        if filas:
            destino.executemany(
                "INSERT INTO state_transitions (ts,symbol,prev_state,new_state,"
                "score,price,direction,escalated,config_fingerprint,code_revision) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", filas)
            destino.commit()
            total += len(filas)
        resto = (time.time() - t0) / i * (len(simbolos) - i)
        print(f"  [{i}/{len(simbolos)}] {sym:16} {len(velas):>7,} velas -> "
              f"{len(filas):>5} transiciones   quedan ~{resto/60:.0f} min", flush=True)

    print(f"\n  total: {total:,} transiciones")
    origen.close(); destino.close()


if __name__ == "__main__":
    main()
