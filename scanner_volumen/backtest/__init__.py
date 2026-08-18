"""Herramienta de backtest: mide, no decide.

Lee `signals` y `signal_outcomes` (ya grabadas por el scanner en marcha) y
responde qué combinación de regla de entrada y horizonte de salida tiene
expectativa positiva -sin nunca convertir eso en una recomendación. Ver
`scanner_volumen/backtest/report.py` para el porqué de esa frontera.
"""
