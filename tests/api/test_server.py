# tests/api/test_server.py
import pytest
from fastapi.testclient import TestClient

from scanner_volumen.api.server import create_app
from scanner_volumen.app.state import ScannerState, SymbolSnapshot
from scanner_volumen.engine.metrics import SymbolMetrics
from scanner_volumen.models import Direction, State
from scanner_volumen.scoring.score import ScoreBreakdown
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import SignalRepo


def metrica_y_breakdown(symbol, score, ts=1000):
    m = SymbolMetrics(
        symbol=symbol, price=6.72, ret_1m=0.8, ret_3m=1.9, ret_5m=3.7,
        ret_15m=5.1, ret_30m=6.2, ret_1h=7.2, ret_24h=13.8,
        rvol_1m_closed=7.8, rvol_1m_live=8.1, rvol_5m=5.1, rvol_session=3.9,
        demand_burst=2.4, vwap=6.43, vwap_distance=4.5, z_return=3.2,
        market_cap=8e7, volume_24h=3.24e7, open_interest=1.0, funding_rate=0.0,
        profile_confidence="high", ts=ts,
    )
    b = ScoreBreakdown(score, score, 35.0, 37.0, 17.0, Direction.LONG,
                        {"rvol_1m": 13.5})
    return m, b


def snapshot(symbol, score, ts=1000):
    m, b = metrica_y_breakdown(symbol, score, ts)
    return SymbolSnapshot(symbol, m, b, State.SIGNAL, updated_ms=ts)


@pytest.fixture
def entorno(tmp_path):
    """Expone el TestClient junto con el SignalRepo que respalda su conexión,
    para que los tests puedan insertar señales directamente en la misma DB
    que sirve la API."""
    estado = ScannerState()
    estado.connected = True
    estado.bootstrap_done = 3
    estado.bootstrap_total = 12
    # se insertan en orden inverso al score: si el endpoint devolviera las
    # filas en orden de inserción en vez de ordenarlas de verdad, este test
    # lo detectaría
    estado.put(snapshot("BBBUSDT", 72.0))
    estado.put(snapshot("AAAUSDT", 89.0))
    conn = open_db(tmp_path / "t.db")
    repo = SignalRepo(conn)
    app = create_app(estado, repo)
    with TestClient(app) as cliente:
        yield cliente, repo
    conn.close()


@pytest.fixture
def cliente(entorno):
    return entorno[0]


def test_state_devuelve_las_filas_ordenadas_por_score(cliente):
    r = cliente.get("/api/state")
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["connected"] is True
    assert [f["symbol"] for f in cuerpo["rows"]] == ["AAAUSDT", "BBBUSDT"]
    assert cuerpo["rows"][0]["score"] == 89.0


def test_state_incluye_el_progreso_del_bootstrap(cliente):
    cuerpo = cliente.get("/api/state").json()
    assert cuerpo["bootstrap"] == {"done": 3, "total": 12}


def test_detalle_de_un_simbolo_incluye_el_desglose(cliente):
    cuerpo = cliente.get("/api/symbol/AAAUSDT").json()
    assert cuerpo["symbol"] == "AAAUSDT"
    assert cuerpo["score_momentum"] == 35.0
    assert cuerpo["components"]["rvol_1m"] == 13.5


def test_detalle_de_un_simbolo_inexistente_da_404(cliente):
    assert cliente.get("/api/symbol/NOEXISTE").status_code == 404


def test_signals_devuelve_lista_vacia_al_principio(cliente):
    assert cliente.get("/api/signals?since=0").json() == []


def test_signals_devuelve_las_senales_guardadas_filtrando_por_since(entorno):
    cliente, repo = entorno
    m_vieja, b_vieja = metrica_y_breakdown("CCCUSDT", 91.0, ts=500)
    m_nueva, b_nueva = metrica_y_breakdown("DDDUSDT", 95.0, ts=2000)
    repo.insert(m_vieja, b_vieja, State.SIGNAL)
    repo.insert(m_nueva, b_nueva, State.SIGNAL)

    cuerpo = cliente.get("/api/signals?since=1000").json()

    assert [f["symbol"] for f in cuerpo] == ["DDDUSDT"]
    assert cuerpo[0]["score"] == 95.0


def test_el_dashboard_se_sirve_en_la_raiz(cliente):
    r = cliente.get("/")
    assert r.status_code == 200
    assert "BITGET" in r.text.upper()


def test_los_estaticos_se_sirven_bajo_slash_static(cliente):
    css = cliente.get("/static/style.css")
    js = cliente.get("/static/app.js")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert js.status_code == 200


def test_el_websocket_envia_el_estado_al_conectar(cliente):
    with cliente.websocket_connect("/ws") as ws:
        datos = ws.receive_json()
        assert datos["rows"][0]["symbol"] == "AAAUSDT"
