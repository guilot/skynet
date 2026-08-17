# tests/engine/test_burst.py
import pytest

from scanner_volumen.engine.burst import demand_burst, z_return


def test_demand_burst_es_el_cociente_de_rvols():
    assert abs(demand_burst(8.4, 3.1) - 2.7097) < 0.001


def test_demand_burst_por_debajo_de_uno_indica_perdida_de_intensidad():
    assert demand_burst(4.0, 8.0) == 0.5


def test_demand_burst_acota_el_denominador():
    """Con RVOL previo de 0.01, el cociente sería 800x y no significa nada."""
    assert demand_burst(8.0, 0.01, min_denominator=0.5) == 16.0


def test_demand_burst_sin_referencia_previa_devuelve_none():
    assert demand_burst(8.4, None) is None


def test_demand_burst_sin_rvol_actual_devuelve_none():
    assert demand_burst(None, 3.1) is None


def test_z_return_mide_desviaciones_tipicas():
    historicos = [0.0, 0.0, 0.0, 0.0, 1.0, -1.0, 1.0, -1.0]
    z = z_return(historicos, current=2.0, min_samples=8)
    assert z is not None and z > 2.0


def test_z_return_de_un_movimiento_normal_es_bajo():
    historicos = [0.1, -0.1, 0.2, -0.2, 0.1, -0.1, 0.15, -0.15]
    z = z_return(historicos, current=0.1, min_samples=8)
    assert z is not None and abs(z) < 1.5


def test_z_return_con_desviacion_cero_devuelve_none():
    """Un símbolo completamente plano no permite calcular un z-score."""
    assert z_return([0.0] * 10, current=1.0, min_samples=8) is None


def test_z_return_con_pocas_muestras_devuelve_none():
    assert z_return([0.1, 0.2], current=1.0, min_samples=8) is None


def test_z_return_exige_min_samples_explicito():
    """Minor: sin ningún default en engine/burst.py que duplique el
    `zscore_min_samples` de config.toml, min_samples es obligatorio."""
    with pytest.raises(TypeError):
        z_return([0.1, 0.2], current=1.0)
