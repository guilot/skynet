# Bitget Real-Time Momentum Scanner

Detecta en tiempo real qué criptomonedas de Bitget USDT-perp están
experimentando una expansión estadísticamente anormal de precio y volumen.

## Uso

```bash
pip install -e ".[dev]"
python -m scanner_volumen
```

Dashboard en http://127.0.0.1:8000

El primer arranque descarga 14 días de velas de 1m por símbolo (~25 minutos con
el universo por defecto). El scanner es utilizable desde el primer minuto: los
símbolos sin perfil completo se marcan con ⚠ y usan una referencia de volumen
menos fiable. El histórico se persiste, así que los reinicios posteriores solo
rellenan el hueco.

## Configuración

Todos los umbrales están en `config.toml`. Los más relevantes:

| Parámetro | Efecto |
|---|---|
| `universe.min_volume_24h` | Prefiltro barato: volumen 24h mínimo para descargar el histórico de un símbolo |
| `universe.min_profile_median_volume` | Puerta real: volumen típico de minuto (perfil) mínimo para quedarse en el universo activo |
| `universe.max_symbols` | Tope de símbolos vigilados a la vez |
| `states.signal` | Score a partir del cual se genera alerta |
| `score.curves.*` | Cómo se traduce cada métrica a puntos |

Los pesos del score suman exactamente 100 y un test lo verifica: si se recalibra
una curva hay que compensar en otra.

## Tests

```bash
pytest
```

La suite no accede a la red: usa respuestas reales de Bitget capturadas en
`tests/fixtures/`.

## Documentación

- Especificación: `docs/superpowers/specs/2026-08-15-bitget-momentum-scanner-design.md`
- Plan de implementación: `docs/superpowers/plans/2026-08-15-bitget-momentum-scanner.md`
- Concepto original: `concepto/bitget_realtime_crypto_momentum_scanner.md`

## Estado

V1. Sin trading automático. Los pesos del score son un punto de partida
razonado, no un sistema validado: cada señal se registra junto con su resultado
posterior a 1, 5, 15, 30 y 60 minutos para poder calibrarlos con datos.
