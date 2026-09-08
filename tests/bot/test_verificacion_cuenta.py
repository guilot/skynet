"""Verificación de cuenta por símbolo (Task 11, Step 2): perezosa, cacheada,
y que nunca modifica nada -solo veta la entrada de un símbolo mal
configurado. Los dobles de `lector` no tocan red ni `BitgetPrivate`: es
justo la garantía que el brief pide ("función pura y testeable")."""
from scanner_volumen.bitget.private import ConfiguracionCuentaSymbol
from scanner_volumen.bot.verificacion_cuenta import MOTIVO_VETO, VerificadorCuenta
from scanner_volumen.strategy.model import StrategyParams


def _config(aislado=True, long_=20.0, short=20.0):
    return ConfiguracionCuentaSymbol(
        margen_aislado=aislado, apalancamiento_long=long_, apalancamiento_short=short,
    )


class LectorFalso:
    """Cuenta cuántas veces se le pregunta por cada símbolo, para probar el
    cacheo sin ninguna dependencia de red."""

    def __init__(self, respuestas: dict[str, ConfiguracionCuentaSymbol]):
        self._respuestas = respuestas
        self.llamadas: list[str] = []

    async def __call__(self, symbol: str) -> ConfiguracionCuentaSymbol:
        self.llamadas.append(symbol)
        return self._respuestas[symbol]


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
