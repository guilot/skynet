# tests/app/test_bootstrap.py
from scanner_volumen.app.bootstrap import Bootstrapper, plan_history_requests
from scanner_volumen.config import ProfileConfig
from scanner_volumen.models import Candle
from scanner_volumen.storage.db import open_db
from scanner_volumen.storage.repos import CandleRepo, ProfileRepo

import pytest

MINUTO = 60_000
DIA = 1440 * MINUTO


def test_planifica_las_paginas_necesarias_desde_cero():
    """14 días de velas de 1m en páginas de 200 son 101 peticiones."""
    paginas = plan_history_requests(latest_ts=None, now_ms=100 * DIA,
                                    history_days=14, page_size=200)
    assert len(paginas) == 101
    # de más reciente a más antigua, espaciadas exactamente page_size minutos
    assert paginas[0] == 100 * DIA
    diferencias = {paginas[i] - paginas[i + 1] for i in range(len(paginas) - 1)}
    assert diferencias == {200 * MINUTO}
    assert paginas[0] > paginas[-1]


def test_solo_planifica_el_hueco_si_ya_hay_historico():
    ahora = 100 * DIA
    paginas = plan_history_requests(latest_ts=ahora - 400 * MINUTO, now_ms=ahora,
                                    history_days=14, page_size=200)
    assert len(paginas) == 2


def test_planifica_paginas_para_un_hueco_multiplo_exacto_del_page_size():
    """Un hueco que es múltiplo exacto de page_size no debe generar una
    página de más: la división entera hacia arriba debe seguir dando el
    número justo de páginas cuando no sobra resto."""
    ahora = 100 * DIA
    # el hueco real es 400 minutos: la vela más nueva ya guardada cubre su
    # propio minuto, así que falta desde (latest_ts + 1 minuto) hasta ahora.
    paginas = plan_history_requests(latest_ts=ahora - 401 * MINUTO, now_ms=ahora,
                                    history_days=14, page_size=200)
    assert len(paginas) == 2


def test_no_planifica_nada_si_el_historico_esta_al_dia():
    ahora = 100 * DIA
    paginas = plan_history_requests(latest_ts=ahora - MINUTO, now_ms=ahora,
                                    history_days=14, page_size=200)
    assert paginas == []


class RestFalso:
    """Devuelve velas sintéticas para cualquier endTime solicitado."""

    def __init__(self):
        self.llamadas = []

    async def get_history_candles(self, symbol, end_time_ms, limit=200):
        self.llamadas.append((symbol, end_time_ms, limit))
        return [
            Candle(ts=end_time_ms - (limit - i) * MINUTO, open=100, high=100,
                   low=100, close=100, base_vol=1.0, quote_vol=100.0)
            for i in range(limit)
        ]


@pytest.fixture
def repos(tmp_path):
    conn = open_db(tmp_path / "t.db")
    yield CandleRepo(conn), ProfileRepo(conn)
    conn.close()


def cfg():
    return ProfileConfig(history_days=2, smoothing_window_minutes=0,
                         min_days_for_confidence=1, rolling_fallback_candles=120,
                         stale_after_hours=24.0)


async def test_bootstrap_descarga_persiste_y_construye_el_perfil(repos):
    velas_repo, perfil_repo = repos
    rest = RestFalso()
    b = Bootstrapper(rest, velas_repo, perfil_repo, cfg())

    perfil = await b.bootstrap_symbol("AAAUSDT", now_ms=100 * DIA)

    assert len(rest.llamadas) > 0
    assert perfil.confidence == "high"
    assert velas_repo.latest_ts("AAAUSDT") is not None
    assert perfil_repo.load("AAAUSDT") is not None


async def test_un_segundo_bootstrap_solo_pide_el_hueco(repos):
    velas_repo, perfil_repo = repos
    rest = RestFalso()
    b = Bootstrapper(rest, velas_repo, perfil_repo, cfg())

    await b.bootstrap_symbol("AAAUSDT", now_ms=100 * DIA)
    primeras = len(rest.llamadas)
    await b.bootstrap_symbol("AAAUSDT", now_ms=100 * DIA + 10 * MINUTO)

    assert len(rest.llamadas) - primeras <= 1


async def test_progress_refleja_los_simbolos_completados(repos):
    velas_repo, perfil_repo = repos
    b = Bootstrapper(RestFalso(), velas_repo, perfil_repo, cfg())
    b.expect(["AAAUSDT", "BBBUSDT"])
    assert b.progress() == (0, 2)
    await b.bootstrap_symbol("AAAUSDT", now_ms=100 * DIA)
    assert b.progress() == (1, 2)


async def test_un_fallo_de_pagina_no_aborta_el_bootstrap(repos):
    velas_repo, perfil_repo = repos

    class RestConFallo(RestFalso):
        async def get_history_candles(self, symbol, end_time_ms, limit=200):
            if len(self.llamadas) == 1:
                self.llamadas.append((symbol, end_time_ms, limit))
                raise RuntimeError("Bitget devolvió code=40020")
            return await super().get_history_candles(symbol, end_time_ms, limit)

    rest = RestConFallo()
    b = Bootstrapper(rest, velas_repo, perfil_repo, cfg())
    ahora = 100 * DIA
    esperadas = plan_history_requests(None, ahora, cfg().history_days)

    perfil = await b.bootstrap_symbol("AAAUSDT", now_ms=ahora)

    assert perfil is not None  # construye el perfil con lo que sí llegó
    # todas las páginas planificadas se intentaron: el fallo de una no
    # detuvo el bucle antes de llegar a las siguientes.
    assert len(rest.llamadas) == len(esperadas)
    # las páginas posteriores al fallo sí se guardaron, no solo la primera.
    assert len(velas_repo.load("AAAUSDT", 0)) > 200
