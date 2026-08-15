"""Conversión del JSON de Bitget a los modelos internos.

Funciones puras: no tocan red ni reloj. Todo test de parseo usa fixtures.
"""
from __future__ import annotations

from scanner_volumen.models import Candle, Contract, Ticker


def parse_candle(row: list[str]) -> Candle:
    """Acepta filas de 7 campos (REST) y de 8 (WebSocket, que añade usdtVolume)."""
    if len(row) not in (7, 8):
        raise ValueError(f"la vela debe tener 7 u 8 campos, recibidos {len(row)}: {row!r}")
    return Candle(
        ts=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        base_vol=float(row[5]),
        quote_vol=float(row[6]),
    )


def parse_candles(payload: dict) -> list[Candle]:
    velas = [parse_candle(row) for row in payload["data"]]
    velas.sort(key=lambda c: c.ts)
    return velas


def parse_contracts(payload: dict) -> list[Contract]:
    return [
        Contract(
            symbol=row["symbol"],
            base_coin=row["baseCoin"],
            symbol_type=row["symbolType"],
            status=row["symbolStatus"],
            is_rwa=row["isRwa"] == "YES",
        )
        for row in payload["data"]
    ]


def parse_tickers(payload: dict) -> list[Ticker]:
    return [
        Ticker(
            symbol=row["symbol"],
            last=float(row["lastPr"]),
            # change24h llega como fracción (0.0016 = 0.16%)
            change_24h=float(row["change24h"]) * 100,
            volume_24h_usdt=float(row["usdtVolume"]),
            open_interest=float(row.get("holdingAmount") or 0.0),
            funding_rate=float(row.get("fundingRate") or 0.0),
            ts=int(row["ts"]),
        )
        for row in payload["data"]
    ]
