# tests/api/test_server.py
import pytest
from fastapi.testclient import TestClient

from scanner_volumen.api.server import create_app
from scanner_volumen.app.state import ScannerState, SymbolSnapshot
from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.engine.metrics import SymbolMetrics
from scanner_volumen.models import Direction, State
from scanner_volumen.scoring.score import ScoreBreakdown
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import SignalRepo
from scanner_volumen.strategy.model import ExitReason


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


def test_state_incluye_ws_connected_y_el_umbral_de_obsolescencia(entorno):
    """I1: `ws_connected` (movido desde BitgetWebsocket.run) y
    `stale_after_ms` (para que el dashboard marque filas obsoletas) deben
    viajar en el mismo payload que el resto del estado."""
    cliente, _ = entorno
    cuerpo = cliente.get("/api/state").json()
    assert cuerpo["ws_connected"] is False  # nada lo puso a True en este test
    assert cuerpo["stale_after_ms"] == 30_000  # valor por defecto de ScannerState


def test_state_incluye_now_ms_del_reloj_del_exchange():
    """Regresión I-2(a): `app.js` comparaba `Date.now()` -el reloj del
    NAVEGADOR- contra `updated_ms` -el reloj del EXCHANGE-, el mismo tipo de
    seam de dos relojes que motivó I6/C-1. El servidor debe mandar su propio
    `now_ms` (el mismo reloj del exchange que el resto del sistema, nunca
    `time.time()`) para que la comparación de obsolescencia se haga contra
    el mismo reloj en los dos lados, no contra el del navegador."""
    estado = ScannerState()
    estado.now_ms = 1_234_567
    assert estado.to_dict()["now_ms"] == 1_234_567


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
    repo.insert(m_vieja, b_vieja, State.SIGNAL,
                config_fingerprint="a" * 64, code_revision="test-rev")
    repo.insert(m_nueva, b_nueva, State.SIGNAL,
                config_fingerprint="a" * 64, code_revision="test-rev")

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


def test_api_bot_sin_bot_devuelve_desactivado(cliente):
    """`entorno` construye la app sin `bot_repo` (valor por defecto `None`,
    el caso de siempre hasta ahora con `bot.enabled = false`): la ruta debe
    responder desactivada sin reventar."""
    r = cliente.get("/api/bot")
    assert r.status_code == 200
    assert r.json() == {
        "activo": False, "equity": None, "saldo_real": None,
        "abiertas": [], "cerradas": [],
    }


def test_api_bot_publica_equity_y_abiertas(tmp_path):
    """Reutiliza los mismos dobles que `entorno` (un `ScannerState` en
    blanco y un `SignalRepo` sobre una conexión propia), pero con un
    `BotRepo` real -sobre la misma conexión sqlite- para comprobar que la
    ruta serializa equity, posiciones abiertas y posiciones cerradas tal
    cual las guarda el bot, sin recalcular nada. Cubre además el recorte y
    el orden de `cerradas` (Ronda 1: antes solo se probaba con la lista
    vacía, así que un bug en el mapeo de campos o en el orden expuesto
    habría pasado desapercibido)."""
    estado = ScannerState()
    conn = open_db(tmp_path / "scanner.db")
    signal_repo = SignalRepo(conn)
    bot_repo = BotRepo(conn)
    bot_repo.set_equity_inicial("paper", 1000.0)
    bot_repo.abrir(
        modo="paper", symbol="AAAUSDT", direction=Direction.LONG, entry_ts=0,
        entry_price=100.0, entry_price_senal=100.0, margin=20.0,
        notional=400.0, size=4.0, fee_entrada=0.24,
    )
    pid_cerrada = bot_repo.abrir(
        modo="paper", symbol="BBBUSDT", direction=Direction.LONG, entry_ts=0,
        entry_price=50.0, entry_price_senal=50.0, margin=10.0,
        notional=200.0, size=4.0, fee_entrada=0.12,
    )
    bot_repo.cerrar(pid_cerrada, close_ts=60_000, pnl=25.0, fees=0.3, max_rank=3)
    app = create_app(estado, signal_repo, bot_repo=bot_repo, modo="paper")
    with TestClient(app) as cliente:
        datos = cliente.get("/api/bot").json()
    conn.close()

    assert datos["activo"] is True
    assert datos["modo"] == "paper"
    assert datos["equity"] == pytest.approx(1025.0)
    assert datos["saldo_real"] is None  # nunca se persiste en paper
    assert len(datos["abiertas"]) == 1
    assert datos["abiertas"][0]["symbol"] == "AAAUSDT"
    assert datos["abiertas"][0]["precio"] is None
    # El historico que consume el panel: ademas de lo que ya habia, lleva
    # direccion, precio y momento de entrada -de ahi sale la duracion- y el
    # flag `degradada`, porque una posicion cerrada tras un fallo del broker
    # no es un trade normal y el panel no debe presentarla como tal.
    fila = datos["cerradas"][0]
    assert fila["symbol"] == "BBBUSDT"
    assert fila["direction"] == "LONG"
    assert fila["entry_price"] == pytest.approx(50.0)
    assert fila["close_ts"] == 60_000
    assert fila["pnl"] == pytest.approx(25.0)
    assert fila["margin"] == pytest.approx(10.0)
    assert fila["max_rank"] == 3
    assert fila["degradada"] is False
    # `fees` es el coste TOTAL del trade (entrada + salidas), no solo el de
    # las salidas: es lo que el operador quiere comparar contra el PnL. Aqui
    # la fila tiene fee_entrada=0.12 y fees=0.30, asi que el total es 0.42
    # -si esta asercion dijera 0.30, estaria midiendo la mitad del coste.
    assert fila["fees"] == pytest.approx(0.42)
    # Sin fills registrados no hay precio de salida que promediar, y se dice
    # con un `None` en vez de inventar el de entrada.
    assert fila["exit_price"] is None
    assert fila["fases"] == 0


def test_api_bot_publica_el_saldo_real_persistido_en_modo_real(tmp_path):
    """Task 11, ronda de arreglo: el panel avisa de "DINERO REAL" (Step 4)
    junto a un número que, sin este campo, era el equity CONTABLE -exponer
    el saldo real es lo que le permite al frontend mostrar el número
    correcto en vez de uno que parece real pero no lo es."""
    estado = ScannerState()
    conn = open_db(tmp_path / "scanner.db")
    signal_repo = SignalRepo(conn)
    bot_repo = BotRepo(conn)
    bot_repo.set_equity_inicial("real", 1000.0)
    bot_repo.set_saldo_real("real", 995.0)
    app = create_app(estado, signal_repo, bot_repo=bot_repo, modo="real")
    with TestClient(app) as cliente:
        datos = cliente.get("/api/bot").json()
    conn.close()

    assert datos["saldo_real"] == pytest.approx(995.0)


def test_api_bot_recorta_cerradas_a_veinte_mas_recientes(tmp_path):
    """La ruta pide `bot_repo.cerradas(modo, limite=20)`: comprueba que el
    recorte llega hasta la API (no solo que `BotRepo.cerradas` lo respete
    en aislado, ya probado en `tests/bot/test_repo.py`) y que expone las
    más recientes primero."""
    estado = ScannerState()
    conn = open_db(tmp_path / "scanner.db")
    signal_repo = SignalRepo(conn)
    bot_repo = BotRepo(conn)
    bot_repo.set_equity_inicial("paper", 1000.0)
    for i in range(25):
        pid = bot_repo.abrir(
            modo="paper", symbol=f"S{i}USDT", direction=Direction.LONG,
            entry_ts=i * 60_000, entry_price=10.0, entry_price_senal=10.0,
            margin=10.0, notional=100.0, size=10.0, fee_entrada=0.05,
        )
        bot_repo.cerrar(pid, close_ts=(i + 1) * 60_000, pnl=1.0, fees=0.0, max_rank=1)
    app = create_app(estado, signal_repo, bot_repo=bot_repo, modo="paper")
    with TestClient(app) as cliente:
        datos = cliente.get("/api/bot").json()
    conn.close()

    assert len(datos["cerradas"]) == 20
    assert datos["cerradas"][0]["symbol"] == "S24USDT"  # la más reciente, primero
    assert datos["cerradas"][-1]["symbol"] == "S5USDT"


# --- desglose de un trade por fases ---


def _trade_escalonado(bot_repo):
    """Un LONG que escala a HOT y luego sale por stop, con deslizamiento en
    la primera parcial: el mercado dio 109.0 donde la regla pedia 110.0."""
    pid = bot_repo.abrir(
        modo="paper", symbol="AAAUSDT", direction=Direction.LONG, entry_ts=0,
        entry_price=100.0, entry_price_senal=100.0, margin=20.0,
        notional=400.0, size=4.0, fee_entrada=0.20,
    )
    bot_repo.registrar_fill(pid, ts=60_000, reason=ExitReason.SCALE_HOT,
                            fraction=0.33, precio_referencia=110.0, precio=109.0,
                            comision=0.14, precio_regla=110.0)
    bot_repo.registrar_fill(pid, ts=120_000, reason=ExitReason.STOP,
                            fraction=0.67, precio_referencia=100.0, precio=100.0,
                            comision=0.27, precio_regla=100.0)
    return pid


def test_api_bot_trade_desglosa_las_fases_con_su_pnl(tmp_path):
    """Cuanto se cobro en cada fase, a que precio y con cuanto PnL.

    El PnL por fase se reconstruye (no esta guardado), asi que lo que este
    test protege de verdad es que la suma de las fases mas la comision de
    entrada reproduzca EXACTAMENTE el pnl que el bot guardo: si algun dia
    divergen, el desglose estaria contando una historia distinta de la del
    libro contable."""
    conn = open_db(tmp_path / "scanner.db")
    bot_repo = BotRepo(conn)
    bot_repo.set_equity_inicial("paper", 1000.0)
    pid = _trade_escalonado(bot_repo)
    # (109-100)*4*0.33 - 0.14 = 11.88 - 0.14 = 11.74
    # (100-100)*4*0.67 - 0.27 =  0.00 - 0.27 = -0.27
    # total - fee_entrada 0.20 = 11.27
    bot_repo.cerrar(pid, close_ts=120_000, pnl=11.27, fees=0.41, max_rank=2)

    app = create_app(ScannerState(), SignalRepo(conn), bot_repo=bot_repo, modo="paper")
    with TestClient(app) as cliente:
        d = cliente.get(f"/api/bot/trade/{pid}").json()

    assert [f["reason"] for f in d["fases"]] == ["SCALE_HOT", "STOP"]
    assert d["fases"][0]["fraction"] == pytest.approx(0.33)
    assert d["fases"][0]["precio"] == pytest.approx(109.0)
    assert d["fases"][0]["pnl_neto"] == pytest.approx(11.74)
    assert d["fases"][1]["pnl_neto"] == pytest.approx(-0.27)
    # el deslizamiento: la regla pedia 110, el mercado dio 109 -> -91 bps
    assert d["fases"][0]["desvio_bps"] == pytest.approx(-90.909, rel=1e-3)
    # y el coste TOTAL, entrada incluida
    assert d["fees_total"] == pytest.approx(0.61)

    reconstruido = sum(f["pnl_neto"] for f in d["fases"]) - d["fee_entrada"]
    assert reconstruido == pytest.approx(d["pnl"], abs=0.01), (
        "el desglose por fases no cuadra con el pnl guardado en la posicion"
    )


def test_api_bot_trade_de_otro_modo_no_se_expone(tmp_path):
    """Los libros de `paper` y `real` son distintos y no deben mezclarse en
    la misma vista: pedir por id un trade de otro modo es un 404, no una
    fila de otro libro."""
    conn = open_db(tmp_path / "scanner.db")
    bot_repo = BotRepo(conn)
    pid = bot_repo.abrir(
        modo="real", symbol="AAAUSDT", direction=Direction.LONG, entry_ts=0,
        entry_price=100.0, entry_price_senal=100.0, margin=20.0,
        notional=400.0, size=4.0, fee_entrada=0.2,
    )
    bot_repo.cerrar(pid, close_ts=60_000, pnl=1.0, fees=0.1, max_rank=2)

    app = create_app(ScannerState(), SignalRepo(conn), bot_repo=bot_repo, modo="paper")
    with TestClient(app) as cliente:
        assert cliente.get(f"/api/bot/trade/{pid}").status_code == 404
        assert cliente.get("/api/bot/trade/9999").status_code == 404
