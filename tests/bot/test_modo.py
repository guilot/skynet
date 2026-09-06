import pytest

from scanner_volumen.bot.modo import PAPER, REAL, REAL_LECTURA, resolver_modo
from scanner_volumen.config import BotConfig


def cfg(modo="paper"):
    return BotConfig(enabled=True, modo=modo, equity_inicial=1000.0,
                     desvio_max_entrada=0.0)


def test_paper_ignora_la_variable_de_entorno():
    assert resolver_modo(cfg("paper"), {}) == PAPER
    assert resolver_modo(cfg("paper"), {"SCANNER_BOT_REAL": "ordenes"}) == PAPER


def test_real_sin_variable_no_arranca():
    with pytest.raises(ValueError, match="SCANNER_BOT_REAL"):
        resolver_modo(cfg("real"), {})


def test_real_con_lectura_da_modo_de_solo_lectura():
    assert resolver_modo(cfg("real"), {"SCANNER_BOT_REAL": "lectura"}) == REAL_LECTURA


def test_real_con_ordenes_da_modo_real():
    assert resolver_modo(cfg("real"), {"SCANNER_BOT_REAL": "ordenes"}) == REAL


def test_un_valor_desconocido_de_la_variable_no_arranca():
    # fallar cerrado: cualquier cosa que no sea exactamente uno de los dos
    # valores previstos deja el bot sin arrancar, no en modo real
    with pytest.raises(ValueError, match="SCANNER_BOT_REAL"):
        resolver_modo(cfg("real"), {"SCANNER_BOT_REAL": "si"})


def test_el_mensaje_dice_que_falta():
    with pytest.raises(ValueError) as exc:
        resolver_modo(cfg("real"), {})
    mensaje = str(exc.value)
    assert "lectura" in mensaje and "ordenes" in mensaje
