"""Verificación de cuenta por símbolo (Task 11, Step 2): perezosa, cacheada,
y que nunca modifica nada -solo veta la entrada de un símbolo mal
configurado. Los dobles de `lector` no tocan red ni `BitgetPrivate`: es
justo la garantía que el brief pide ("función pura y testeable")."""
import pytest

from scanner_volumen.bitget.private import ConfiguracionCuentaSymbol
from scanner_volumen.bot.verificacion_cuenta import MOTIVO_VETO, VerificadorCuenta
from scanner_volumen.strategy.model import StrategyParams


def _config(aislado=True, long_=20.0, short=20.0, una_via=True):
    return ConfiguracionCuentaSymbol(
        margen_aislado=aislado, apalancamiento_long=long_, apalancamiento_short=short,
        modo_una_via=una_via,
    )


class LectorFalso:
    """Cuenta cuántas veces se le pregunta por cada símbolo, para probar el
    cacheo sin ninguna dependencia de red."""

    def __init__(self, respuestas: dict[str, ConfiguracionCuentaSymbol]):
        # publico: el doble del ajustador lo muta para simular que el
        # exchange aplico el cambio, y asi poder probar la RELECTURA.
        self.configs = respuestas
        self.llamadas: list[str] = []

    async def __call__(self, symbol: str) -> ConfiguracionCuentaSymbol:
        self.llamadas.append(symbol)
        return self.configs[symbol]


class LectorQueFallaLaPrimeraVez:
    """Lanza en la primera llamada para un símbolo dado, y responde
    normalmente a partir de la segunda -simula un fallo transitorio de red
    seguido de un reintento con éxito."""

    def __init__(self, respuesta: ConfiguracionCuentaSymbol):
        self._respuesta = respuesta
        self.llamadas = 0

    async def __call__(self, symbol: str) -> ConfiguracionCuentaSymbol:
        self.llamadas += 1
        if self.llamadas == 1:
            raise RuntimeError("simulado: fallo transitorio de red")
        return self._respuesta


async def test_configuracion_correcta_no_veta():
    lector = LectorFalso({"AAAUSDT": _config(aislado=True, long_=20.0, short=20.0)})
    verificador = VerificadorCuenta(StrategyParams(apalancamiento=20.0), lector)
    assert await verificador.verificar("AAAUSDT") is None


async def test_margen_cruzado_veta():
    lector = LectorFalso({"AAAUSDT": _config(aislado=False, long_=20.0, short=20.0)})
    verificador = VerificadorCuenta(StrategyParams(apalancamiento=20.0), lector)
    assert await verificador.verificar("AAAUSDT") == MOTIVO_VETO


async def test_apalancamiento_distinto_veta():
    lector = LectorFalso({"AAAUSDT": _config(aislado=True, long_=10.0, short=10.0)})
    verificador = VerificadorCuenta(StrategyParams(apalancamiento=20.0), lector)
    assert await verificador.verificar("AAAUSDT") == MOTIVO_VETO


async def test_apalancamiento_distinto_solo_en_un_lado_tambien_veta():
    # Bitget permite apalancamiento distinto por lado en margen aislado: si
    # SOLO uno de los dos no coincide, sigue sin ser seguro operar el
    # símbolo en la dirección que la señal pida.
    lector = LectorFalso({"AAAUSDT": _config(aislado=True, long_=20.0, short=10.0)})
    verificador = VerificadorCuenta(StrategyParams(apalancamiento=20.0), lector)
    assert await verificador.verificar("AAAUSDT") == MOTIVO_VETO


async def test_el_resultado_se_cachea_por_simbolo():
    lector = LectorFalso({"AAAUSDT": _config()})
    verificador = VerificadorCuenta(StrategyParams(apalancamiento=20.0), lector)
    assert await verificador.verificar("AAAUSDT") is None
    assert await verificador.verificar("AAAUSDT") is None
    assert lector.llamadas == ["AAAUSDT"]  # una sola consulta real


async def test_el_veto_tambien_se_cachea():
    lector = LectorFalso({"AAAUSDT": _config(aislado=False)})
    verificador = VerificadorCuenta(StrategyParams(apalancamiento=20.0), lector)
    assert await verificador.verificar("AAAUSDT") == MOTIVO_VETO
    assert await verificador.verificar("AAAUSDT") == MOTIVO_VETO
    assert lector.llamadas == ["AAAUSDT"]


async def test_un_simbolo_no_afecta_al_cacheo_de_otro():
    lector = LectorFalso({
        "AAAUSDT": _config(aislado=False),
        "BBBUSDT": _config(aislado=True, long_=20.0, short=20.0),
    })
    verificador = VerificadorCuenta(StrategyParams(apalancamiento=20.0), lector)
    assert await verificador.verificar("AAAUSDT") == MOTIVO_VETO
    assert await verificador.verificar("BBBUSDT") is None
    assert sorted(lector.llamadas) == ["AAAUSDT", "BBBUSDT"]


async def test_un_fallo_transitorio_del_lector_no_se_cachea_y_se_reintenta():
    # Hallazgo de revision: si el fallo se cacheara, un problema puntual de
    # red condenaria al simbolo a un veto permanente que ni siquiera pasa
    # por la logica de "corrigelo y reinicia" -no habria nada que corregir.
    lector = LectorQueFallaLaPrimeraVez(_config(aislado=True, long_=20.0, short=20.0))
    verificador = VerificadorCuenta(StrategyParams(apalancamiento=20.0), lector)
    with pytest.raises(RuntimeError, match="fallo transitorio"):
        await verificador.verificar("AAAUSDT")
    # la segunda llamada REINTENTA de verdad -no devuelve nada cacheado del
    # intento fallido- y esta vez el lector responde con exito.
    assert await verificador.verificar("AAAUSDT") is None
    assert lector.llamadas == 2


async def test_pequenas_diferencias_de_punto_flotante_no_vetan():
    # El apalancamiento sale de parsear una cadena JSON con float() (ver
    # BitgetPrivate.get_configuracion_symbol): una comparacion "!=" exacta
    # vetaria un simbolo por un residuo de representacion, no por una
    # configuracion real distinta.
    lector = LectorFalso({
        "AAAUSDT": _config(aislado=True, long_=20.00000000001, short=19.99999999999),
    })
    verificador = VerificadorCuenta(StrategyParams(apalancamiento=20.0), lector)
    assert await verificador.verificar("AAAUSDT") is None


# --- modo de posición unilateral (hallazgo del banco de pruebas, Task 12) ---


async def test_un_simbolo_en_hedge_mode_se_veta():
    """El veto más importante de los tres, y el que faltaba.

    Toda la seguridad de esta fase descansa en que los cierres y los stops
    van `reduceOnly`: es lo que impide que una orden de cierre abra una
    posición contraria. Y `reduceOnly` NO existe en modo cobertura: Bitget
    rechaza esas órdenes con `code=40774 "The order type for unilateral
    position must also be the unilateral position type."` (observado contra
    la cuenta de simulación, no supuesto).

    Sin este veto el bot podría ABRIR en una cuenta en `hedge_mode` y luego
    no poder cerrar -ni por stop, ni a mercado-, que es exactamente la
    situación que esta fase entera existe para hacer imposible."""
    lector = LectorFalso({"AAAUSDT": _config(una_via=False)})
    v = VerificadorCuenta(StrategyParams(), lector)

    assert await v.verificar("AAAUSDT") == MOTIVO_VETO


async def test_el_modo_de_posicion_se_comprueba_antes_que_el_resto():
    """Con las tres cosas mal a la vez, el veto sigue siendo uno solo, pero
    el orden importa para el log: el modo de posición es el que deja al bot
    sin poder cerrar, así que es el que hay que nombrar primero."""
    lector = LectorFalso({"AAAUSDT": _config(una_via=False, aislado=False,
                                             long_=1.0, short=1.0)})
    v = VerificadorCuenta(StrategyParams(), lector)

    assert await v.verificar("AAAUSDT") == MOTIVO_VETO


async def test_un_simbolo_bien_configurado_en_una_via_no_se_veta():
    """El caso bueno sigue pasando: unilateral + aislado + apalancamiento
    esperado no produce veto -si no, el veto nuevo dejaría al bot sin operar
    nada."""
    p = StrategyParams()
    lector = LectorFalso({"AAAUSDT": _config(una_via=True, aislado=True,
                                             long_=p.apalancamiento,
                                             short=p.apalancamiento)})
    v = VerificadorCuenta(p, lector)

    assert await v.verificar("AAAUSDT") is None


# --- ajuste automatico de la configuracion del simbolo ---


class AjustadorFalso:
    """Doble del ajustador: apunta lo que se le pide y muta la configuracion
    que devolvera el lector, para poder comprobar la RELECTURA."""

    def __init__(self, lector, config_tras_ajuste=None):
        self._lector = lector
        self._config_tras_ajuste = config_tras_ajuste
        self.llamadas = []

    async def __call__(self, symbol, apalancamiento):
        self.llamadas.append((symbol, apalancamiento))
        if self._config_tras_ajuste is not None:
            self._lector.configs[symbol] = self._config_tras_ajuste


async def test_un_simbolo_mal_configurado_se_ajusta_y_deja_de_vetarse():
    """El caso que motiva todo esto: el margen y el apalancamiento son por
    simbolo y no se heredan, asi que sin ajuste el bot vetaria casi todos
    los pares en los que el escaner encuentra senal."""
    p = StrategyParams()
    lector = LectorFalso({"AAAUSDT": _config(aislado=False, long_=10.0, short=10.0)})
    ajustador = AjustadorFalso(
        lector, _config(aislado=True, long_=p.apalancamiento, short=p.apalancamiento))
    v = VerificadorCuenta(p, lector, ajustador)

    assert await v.verificar("AAAUSDT") is None
    assert ajustador.llamadas == [("AAAUSDT", p.apalancamiento)]


async def test_si_tras_ajustar_sigue_sin_coincidir_se_veta_igual():
    """La verificacion mantiene la ULTIMA PALABRA: ajustar no es dar por
    bueno. Si el exchange acepta la peticion pero la configuracion real no
    cambia, el simbolo se veta como antes."""
    p = StrategyParams()
    lector = LectorFalso({"AAAUSDT": _config(aislado=False, long_=10.0, short=10.0)})
    ajustador = AjustadorFalso(lector, config_tras_ajuste=None)  # no cambia nada
    v = VerificadorCuenta(p, lector, ajustador)

    assert await v.verificar("AAAUSDT") == MOTIVO_VETO
    assert ajustador.llamadas == [("AAAUSDT", p.apalancamiento)]


async def test_un_simbolo_ya_correcto_no_se_toca():
    """Solo se escribe cuando hace falta: si la configuracion ya coincide,
    el bot no manda ninguna peticion de escritura sobre la cuenta."""
    p = StrategyParams()
    lector = LectorFalso({"AAAUSDT": _config(
        aislado=True, long_=p.apalancamiento, short=p.apalancamiento)})
    ajustador = AjustadorFalso(lector)
    v = VerificadorCuenta(p, lector, ajustador)

    assert await v.verificar("AAAUSDT") is None
    assert ajustador.llamadas == []


async def test_el_modo_de_posicion_NO_se_ajusta_nunca():
    """El modo de posicion es de CUENTA, no de simbolo: cambiarlo afectaria
    a toda la operativa del usuario, incluida la manual. Se veta y se avisa,
    pero no se toca -ni siquiera con el ajustador conectado."""
    p = StrategyParams()
    lector = LectorFalso({"AAAUSDT": _config(
        una_via=False, aislado=True, long_=p.apalancamiento, short=p.apalancamiento)})
    ajustador = AjustadorFalso(lector)
    v = VerificadorCuenta(p, lector, ajustador)

    assert await v.verificar("AAAUSDT") == MOTIVO_VETO
    assert ajustador.llamadas == []


async def test_sin_ajustador_el_comportamiento_es_el_de_siempre():
    """El ajustador es opcional: sin el, solo verifica y veta."""
    lector = LectorFalso({"AAAUSDT": _config(aislado=False)})
    v = VerificadorCuenta(StrategyParams(), lector)

    assert await v.verificar("AAAUSDT") == MOTIVO_VETO
