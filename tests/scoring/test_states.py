from scanner_volumen.config import StatesConfig
from scanner_volumen.models import State
from scanner_volumen.scoring.states import StateMachine

MINUTO = 60_000


def cfg(**kwargs):
    base = dict(watch=50, hot=65, signal=80, extreme=90,
                exit_margin=5, exit_ticks=3, cooldown_minutes=15)
    base.update(kwargs)
    return StatesConfig(**base)


def test_empieza_en_normal():
    sm = StateMachine(cfg())
    assert sm.state_of("AAAUSDT") is State.NORMAL


def test_sube_de_estado_al_cruzar_el_umbral():
    sm = StateMachine(cfg())
    t = sm.update("AAAUSDT", 82.0, now_ms=0)
    assert t is not None
    assert t.current is State.SIGNAL
    assert t.previous is State.NORMAL
    assert t.escalated is True


def test_sin_cambio_de_estado_no_hay_transicion():
    sm = StateMachine(cfg())
    sm.update("AAAUSDT", 82.0, now_ms=0)
    assert sm.update("AAAUSDT", 83.0, now_ms=MINUTO) is None


def test_no_baja_de_estado_dentro_del_margen():
    """Un score de 77 tras SIGNAL (80) está dentro del margen de 5 puntos."""
    sm = StateMachine(cfg())
    sm.update("AAAUSDT", 82.0, now_ms=0)
    for i in range(1, 10):
        assert sm.update("AAAUSDT", 77.0, now_ms=i * MINUTO) is None
    assert sm.state_of("AAAUSDT") is State.SIGNAL


def test_baja_de_estado_tras_exit_ticks_por_debajo_del_margen():
    sm = StateMachine(cfg(exit_ticks=3))
    sm.update("AAAUSDT", 82.0, now_ms=0)
    assert sm.update("AAAUSDT", 70.0, now_ms=1 * MINUTO) is None
    assert sm.update("AAAUSDT", 70.0, now_ms=2 * MINUTO) is None
    t = sm.update("AAAUSDT", 70.0, now_ms=3 * MINUTO)
    assert t is not None
    assert t.current is State.HOT
    assert t.escalated is False


def test_el_contador_de_salida_se_reinicia_si_el_score_se_recupera():
    sm = StateMachine(cfg(exit_ticks=3))
    sm.update("AAAUSDT", 82.0, now_ms=0)
    sm.update("AAAUSDT", 70.0, now_ms=1 * MINUTO)
    sm.update("AAAUSDT", 70.0, now_ms=2 * MINUTO)
    sm.update("AAAUSDT", 85.0, now_ms=3 * MINUTO)   # se recupera
    assert sm.update("AAAUSDT", 70.0, now_ms=4 * MINUTO) is None
    assert sm.state_of("AAAUSDT") is State.SIGNAL


def test_alerta_al_entrar_en_signal():
    sm = StateMachine(cfg())
    t = sm.update("AAAUSDT", 82.0, now_ms=0)
    assert t.should_alert is True


def test_no_alerta_al_entrar_en_watch():
    sm = StateMachine(cfg())
    t = sm.update("AAAUSDT", 55.0, now_ms=0)
    assert t.current is State.WATCH
    assert t.should_alert is False


def test_el_cooldown_bloquea_una_segunda_alerta_del_mismo_estado():
    sm = StateMachine(cfg(cooldown_minutes=15, exit_ticks=1))
    sm.update("AAAUSDT", 82.0, now_ms=0)                   # alerta
    sm.update("AAAUSDT", 60.0, now_ms=1 * MINUTO)          # baja a WATCH
    t = sm.update("AAAUSDT", 82.0, now_ms=5 * MINUTO)      # vuelve a SIGNAL
    assert t.current is State.SIGNAL
    assert t.should_alert is False  # dentro del cooldown


def test_el_escalado_ignora_el_cooldown():
    """Si empeora la situación y pasa a EXTREME, hay que avisar igualmente."""
    sm = StateMachine(cfg(cooldown_minutes=15))
    sm.update("AAAUSDT", 82.0, now_ms=0)
    t = sm.update("AAAUSDT", 95.0, now_ms=2 * MINUTO)
    assert t.current is State.EXTREME
    assert t.should_alert is True


def test_el_escalado_ignora_el_cooldown_aunque_haya_bajado_de_estado_antes():
    """Lo que decide el bypass del cooldown es la severidad de la última
    alerta emitida, no el estado inmediatamente anterior a esta transición.

    Si tras alertar en SIGNAL el símbolo baja a WATCH (histéresis con
    exit_ticks=1) y luego escala directamente a EXTREME, sigue siendo una
    situación peor que la última alerta y debe avisar aunque el estado
    anterior a este tick no fuera ya de nivel alerta. Una implementación que
    solo mire el estado inmediatamente anterior (en vez de la última
    severidad alertada) fallaría este test.
    """
    sm = StateMachine(cfg(cooldown_minutes=15, exit_ticks=1))
    sm.update("AAAUSDT", 82.0, now_ms=0)                    # alerta en SIGNAL
    sm.update("AAAUSDT", 60.0, now_ms=1 * MINUTO)           # baja a WATCH
    t = sm.update("AAAUSDT", 95.0, now_ms=2 * MINUTO)       # escala a EXTREME
    assert t.current is State.EXTREME
    assert t.should_alert is True


def test_pasado_el_cooldown_vuelve_a_alertar():
    sm = StateMachine(cfg(cooldown_minutes=15, exit_ticks=1))
    sm.update("AAAUSDT", 82.0, now_ms=0)
    sm.update("AAAUSDT", 60.0, now_ms=1 * MINUTO)
    t = sm.update("AAAUSDT", 82.0, now_ms=20 * MINUTO)
    assert t.should_alert is True


def test_cada_simbolo_lleva_su_propio_estado():
    sm = StateMachine(cfg())
    sm.update("AAAUSDT", 82.0, now_ms=0)
    sm.update("BBBUSDT", 30.0, now_ms=0)
    assert sm.state_of("AAAUSDT") is State.SIGNAL
    assert sm.state_of("BBBUSDT") is State.NORMAL
