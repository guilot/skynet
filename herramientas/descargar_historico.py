"""Descarga velas de 1 minuto de Bitget hacia atras, para poder backtestear
mucho mas alla de lo que el escaner lleva recopilando.

    .venv/bin/python herramientas/descargar_historico.py --dias 90 --db data/historico.db

REANUDABLE: cada simbolo se descarga hacia atras desde la vela mas antigua
que ya haya en la base, asi que cortar el proceso y relanzarlo continua donde
lo dejo. Con 90 dias y ~120 simbolos son unas 2 horas: se va a cortar.

El universo se toma de los tickers ACTUALES filtrados por volumen, igual que
hace el escaner. Eso introduce SESGO DE SUPERVIVENCIA -los pares que se
listaron y murieron en el periodo no estan- y sesga el resultado al alza. Va
anotado en `docs/criterio-de-decision.md` para leerlo con eso delante.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import time
from pathlib import Path

import httpx

BASE = "https://api.bitget.com"
MIN_MS = 60_000
POR_PAGINA = 200          # limite de la API
PAUSA = 0.12              # ~8 peticiones/s, holgado frente al limite de Bitget


async def _tickers(http: httpx.AsyncClient) -> list[dict]:
    r = await http.get(f"{BASE}/api/v2/mix/market/tickers",
                       params={"productType": "USDT-FUTURES"})
    r.raise_for_status()
    return r.json().get("data") or []


async def _pagina(http: httpx.AsyncClient, symbol: str, fin_ms: int) -> list[list]:
    """Una pagina de 200 velas que TERMINA en `fin_ms`. Devuelve [] si no hay."""
    r = await http.get(f"{BASE}/api/v2/mix/market/history-candles",
                       params={"symbol": symbol, "productType": "USDT-FUTURES",
                               "granularity": "1m", "limit": str(POR_PAGINA),
                               "endTime": str(fin_ms)})
    if r.status_code != 200:
        return []
    return r.json().get("data") or []


def _preparar(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS candles_1m (
        symbol TEXT NOT NULL, ts INTEGER NOT NULL,
        open REAL, high REAL, low REAL, close REAL,
        base_vol REAL, quote_vol REAL,
        PRIMARY KEY (symbol, ts))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_c_sym_ts ON candles_1m(symbol, ts)")
    conn.commit()


async def descargar(symbol: str, desde_ms: int, http: httpx.AsyncClient,
                    conn: sqlite3.Connection) -> int:
    # Reanudacion: se sigue hacia atras desde lo mas antiguo que ya haya.
    fila = conn.execute("SELECT MIN(ts) FROM candles_1m WHERE symbol=?", (symbol,)).fetchone()
    cursor = fila[0] if fila and fila[0] else int(time.time() * 1000)
    nuevas = 0
    while cursor > desde_ms:
        velas = await _pagina(http, symbol, cursor)
        if not velas:
            break
        filas = []
        for v in velas:
            ts = int(v[0])
            if ts < desde_ms:
                continue
            # [ts, open, high, low, close, base_vol, quote_vol]
            filas.append((symbol, ts, float(v[1]), float(v[2]), float(v[3]),
                          float(v[4]), float(v[5]), float(v[6])))
        if filas:
            conn.executemany(
                "INSERT OR IGNORE INTO candles_1m VALUES (?,?,?,?,?,?,?,?)", filas)
            conn.commit()
            nuevas += len(filas)
        mas_antigua = min(int(v[0]) for v in velas)
        if mas_antigua >= cursor:      # la API deja de retroceder: no hay mas
            break
        cursor = mas_antigua
        await asyncio.sleep(PAUSA)
    return nuevas


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dias", type=int, default=90)
    p.add_argument("--db", type=Path, default=Path("data/historico.db"))
    p.add_argument("--min-volumen", type=float, default=5e6,
                   help="volumen 24h minimo en USDT para entrar al universo")
    args = p.parse_args()

    desde = int((time.time() - args.dias * 86400) * 1000)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    _preparar(conn)

    async with httpx.AsyncClient(timeout=30.0) as http:
        tks = await _tickers(http)
        universo = sorted(
            (t["symbol"] for t in tks
             if float(t.get("usdtVolume") or 0) >= args.min_volumen),
            key=lambda s: s)
        print(f"  universo: {len(universo)} pares con volumen >= {args.min_volumen:,.0f} USDT")
        print(f"  desde: {args.dias} dias atras   base: {args.db}\n")
        t0 = time.time()
        for i, sym in enumerate(universo, 1):
            try:
                n = await descargar(sym, desde, http, conn)
            except Exception as exc:                      # noqa: BLE001
                print(f"  [{i}/{len(universo)}] {sym}: FALLO {exc}")
                continue
            total = conn.execute("SELECT COUNT(*) FROM candles_1m WHERE symbol=?",
                                 (sym,)).fetchone()[0]
            transcurrido = time.time() - t0
            resto = transcurrido / i * (len(universo) - i)
            print(f"  [{i}/{len(universo)}] {sym:16} +{n:>6} nuevas  "
                  f"total {total:>7}  quedan ~{resto/60:.0f} min", flush=True)
    conn.close()
    print("\n  listo")


if __name__ == "__main__":
    asyncio.run(main())
