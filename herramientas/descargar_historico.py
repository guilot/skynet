"""Descarga velas de 1 minuto de Bitget hacia atras, para poder backtestear
mucho mas alla de lo que el escaner lleva recopilando.

    .venv/bin/python herramientas/descargar_historico.py --dias 90 --db data/historico.db

REANUDABLE: cada simbolo sigue hacia atras desde la vela mas antigua que ya
haya en la base, asi que cortar el proceso y relanzarlo continua donde lo
dejo. Con 90 dias son ~2 horas: se va a cortar.

El universo se toma de los tickers ACTUALES filtrados por volumen, igual que
hace el escaner. Eso introduce SESGO DE SUPERVIVENCIA -los pares que se
listaron y murieron en el periodo no estan- y sesga el resultado al alza. Va
anotado tambien en `docs/criterio-de-decision.md`, para leer el resultado con
eso delante.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import time
from pathlib import Path

import httpx

BASE = "https://api.bitget.com"
POR_PAGINA = 200          # limite de la API
PAUSA = 0.05

# Cuantos simbolos se descargan A LA VEZ. El cuello no es el limite de Bitget
# sino la LATENCIA de cada peticion: en serie salian 1,9 paginas/s y 11 HORAS
# para 90 dias (medido, no estimado). Con 6 en paralelo se ronda las 11
# peticiones/s -holgado frente al limite de los endpoints publicos- y baja a
# unas 2 horas.
CONCURRENCIA = 6


async def _tickers(http: httpx.AsyncClient) -> list[dict]:
    r = await http.get(f"{BASE}/api/v2/mix/market/tickers",
                       params={"productType": "USDT-FUTURES"})
    r.raise_for_status()
    return r.json().get("data") or []


async def _pagina(http: httpx.AsyncClient, symbol: str, fin_ms: int) -> list[list]:
    """Una pagina de 200 velas que TERMINA en `fin_ms`; [] si no hay mas."""
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
                    conn: sqlite3.Connection, escritura: asyncio.Lock) -> int:
    fila = conn.execute("SELECT MIN(ts) FROM candles_1m WHERE symbol=?",
                        (symbol,)).fetchone()
    cursor = fila[0] if fila and fila[0] else int(time.time() * 1000)
    nuevas = 0
    while cursor > desde_ms:
        velas = await _pagina(http, symbol, cursor)
        if not velas:
            break
        filas = [(symbol, int(v[0]), float(v[1]), float(v[2]), float(v[3]),
                  float(v[4]), float(v[5]), float(v[6]))
                 for v in velas if int(v[0]) >= desde_ms]
        if filas:
            # Una sola conexion sqlite compartida por varias corrutinas: los
            # escritores se serializan con el cerrojo. Son corrutinas, no
            # hilos, pero dos `executemany` + `commit` intercalados dejarian
            # transacciones mezcladas.
            async with escritura:
                conn.executemany(
                    "INSERT OR IGNORE INTO candles_1m VALUES (?,?,?,?,?,?,?,?)",
                    filas)
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
        universo = sorted(t["symbol"] for t in tks
                          if float(t.get("usdtVolume") or 0) >= args.min_volumen)
        print(f"  universo: {len(universo)} pares con volumen >= "
              f"{args.min_volumen:,.0f} USDT", flush=True)
        print(f"  {args.dias} dias atras   base: {args.db}   "
              f"concurrencia: {CONCURRENCIA}\n", flush=True)

        t0 = time.time()
        escritura = asyncio.Lock()
        sem = asyncio.Semaphore(CONCURRENCIA)
        hechos = 0

        async def uno(sym: str) -> None:
            nonlocal hechos
            async with sem:
                try:
                    n = await descargar(sym, desde, http, conn, escritura)
                except Exception as exc:                   # noqa: BLE001
                    print(f"  {sym}: FALLO {exc}", flush=True)
                    return
            hechos += 1
            resto = (time.time() - t0) / hechos * (len(universo) - hechos)
            print(f"  [{hechos}/{len(universo)}] {sym:16} +{n:>6}  "
                  f"quedan ~{resto/60:.0f} min", flush=True)

        await asyncio.gather(*(uno(s) for s in universo))
    conn.close()
    print("\n  listo", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
