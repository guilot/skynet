# tests/test_integration.py
"""Reproduce la sesión WebSocket grabada a través del sistema completo.

Es la prueba que detecta que un cambio en cualquier módulo rompió la cadena
entera. No toca la red: usa los fixtures capturados de Bitget.
"""
import json
from pathlib import Path

import pytest

from scanner_volumen.app.orchestrator import Orchestrator
from scanner_volumen.bitget.parsing import parse_contracts, parse_tickers
from scanner_volumen.bitget.ws import decode_message
from scanner_volumen.config import load_config
from scanner_volumen.engine.profile import build_profile
from scanner_volumen.models import Candle
from scanner_volumen.scoring.score import (
    CLAVES_DEMAND, CLAVES_MOMENTUM, CLAVES_STRUCTURE,
)
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import CandleRepo, ProfileRepo, SignalRepo
from scanner_volumen.universe.selector import UniverseSelector

FIXTURES = Path(__file__).parent / "fixtures"
# Anclado a la ubicación del propio fichero, no al cwd: sin esto la suite
# solo pasa si pytest se lanza desde la raíz del repo.
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"
MINUTO = 60_000
DIA = 1440 * MINUTO


class SupplyFalso:
    def market_cap(self, symbol):
        return 8e7


class BootstrapperFalso:
    def __init__(self, cfg):
        self._cfg = cfg

    async def bootstrap_symbol(self, symbol, now_ms):
        velas = [
            Candle(ts=d * DIA + m * MINUTO, open=100, high=100, low=100,
                   close=100, base_vol=1.0, quote_vol=100.0)
            for d in range(14) for m in range(1440)
        ]
        return build_profile(symbol, velas, self._cfg)

    def expect(self, symbols):
        pass

    def progress(self):
        return (0, 0)


@pytest.fixture
def orq(tmp_path):
    cfg = load_config(CONFIG_PATH)
    conn = open_db(tmp_path / "t.db")
    yield Orchestrator(
        cfg=cfg, rest=None, ws=None,
        candle_repo=CandleRepo(conn), profile_repo=ProfileRepo(conn),
        signal_repo=SignalRepo(conn), supply=SupplyFalso(),
        bootstrapper=BootstrapperFalso(cfg.profile),
    )
    conn.close()


async def test_la_sesion_ws_grabada_atraviesa_el_sistema_completo(orq):
    mensajes = json.loads((FIXTURES / "ws_candle1m_session.json").read_text())
    tickers = {t.symbol: t for t in parse_tickers(
        json.loads((FIXTURES / "tickers_usdt_futures.json").read_text())
    )}

    procesados = 0
    ultimo_ts = 0
    for crudo in mensajes:
        evento = decode_message(json.dumps(crudo))
        if evento is None or evento.kind not in ("snapshot", "update"):
            continue
        if evento.symbol in tickers:
            orq.set_ticker(tickers[evento.symbol])
        await orq.ensure_profile(evento.symbol, now_ms=0)
        await orq.handle_ws_event(evento)
        ultimo_ts = max(ultimo_ts, max(c.ts for c in evento.candles))
        orq.evaluate(now_ms=ultimo_ts + 30_000)
        procesados += 1

    assert procesados == 27  # 3 snapshots + 24 updates
    ranking = orq.state.ranked()
    assert len(ranking) == 3
    assert {s.symbol for s in ranking} == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    for s in ranking:
        assert 0 <= s.breakdown.total <= 100
    # el ranking va ordenado descendentemente
    assert all(
        ranking[i].breakdown.total >= ranking[i + 1].breakdown.total
        for i in range(len(ranking) - 1)
    )

    # Aserción dorada: score exacto de SOLUSDT tras reproducir la sesión
    # grabada, derivado ejecutando el pipeline real contra los fixtures (no
    # inventado). Cualquier cambio en engine/, scoring/ o en config.toml que
    # altere el resultado numérico debe mover deliberadamente este valor, no
    # dejarlo pasar en silencio -- las aserciones de rango (0-100) de arriba
    # las pasaría igualmente un score constante, que es justo el defecto
    # (C2/C3) que esta prueba de integración no cazó en su momento.
    sol = next(s for s in ranking if s.symbol == "SOLUSDT")
    assert abs(sol.breakdown.total - 42.56894228594851) < 1e-6

    # Cada uno de los tres bloques del score (MOMENTUM 40 / DEMAND 40 /
    # STRUCTURE 20) debe tener al menos un componente no nulo para cada
    # símbolo. Por sí sola, esta aserción habría destapado C2 (demand_burst
    # inalcanzable con record_rvol por tick en vez de por vela cerrada
    # dejaba rvol_1m/5m/session como únicos aportes de DEMAND, pero un bug
    # que además rompiera esos habría dejado el bloque entero en 0 sin que
    # ningún test lo notara).
    for s in ranking:
        componentes = s.breakdown.components
        assert any(componentes.get(k, 0.0) != 0.0 for k in CLAVES_MOMENTUM), (
            f"{s.symbol}: bloque MOMENTUM completamente en cero"
        )
        assert any(componentes.get(k, 0.0) != 0.0 for k in CLAVES_DEMAND), (
            f"{s.symbol}: bloque DEMAND completamente en cero"
        )
        assert any(componentes.get(k, 0.0) != 0.0 for k in CLAVES_STRUCTURE), (
            f"{s.symbol}: bloque STRUCTURE completamente en cero"
        )


async def test_reproducir_dos_veces_da_el_mismo_resultado(orq, tmp_path):
    """El motor es determinista: mismos datos y mismo now_ms, mismo score."""
    mensajes = json.loads((FIXTURES / "ws_candle1m_session.json").read_text())

    async def reproducir(o):
        ultimo = 0
        for crudo in mensajes:
            ev = decode_message(json.dumps(crudo))
            if ev is None or ev.kind not in ("snapshot", "update"):
                continue
            await o.ensure_profile(ev.symbol, now_ms=0)
            await o.handle_ws_event(ev)
            ultimo = max(ultimo, max(c.ts for c in ev.candles))
            o.evaluate(now_ms=ultimo + 30_000)
        return {s.symbol: s.breakdown.total for s in o.state.ranked()}

    primera = await reproducir(orq)

    cfg = load_config(CONFIG_PATH)
    conn2 = open_db(tmp_path / "t2.db")
    otro = Orchestrator(
        cfg=cfg, rest=None, ws=None,
        candle_repo=CandleRepo(conn2), profile_repo=ProfileRepo(conn2),
        signal_repo=SignalRepo(conn2), supply=SupplyFalso(),
        bootstrapper=BootstrapperFalso(cfg.profile),
    )
    segunda = await reproducir(otro)
    conn2.close()

    assert primera == segunda


def test_el_universo_real_produce_candidatos_sin_rwa():
    cfg = load_config(CONFIG_PATH)
    selector = UniverseSelector(cfg.universe)
    contratos = parse_contracts(
        json.loads((FIXTURES / "contracts_usdt_futures.json").read_text())
    )
    tickers = parse_tickers(
        json.loads((FIXTURES / "tickers_usdt_futures.json").read_text())
    )
    rwa = {c.symbol for c in contratos if c.is_rwa}
    resultado = selector.select(contratos, tickers, now_ms=0)

    assert len(resultado.symbols) > 20
    assert not (resultado.symbols & rwa)
