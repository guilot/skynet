# Bitget Real-Time Crypto Momentum Scanner

## 1. Objetivo

Construir un scanner de criptomonedas en tiempo real utilizando **Bitget Spot** como fuente principal de datos.

El objetivo no es simplemente encontrar las criptomonedas que más han subido, sino detectar:

> **Qué criptomonedas están experimentando un aumento anormal de demanda y momentum en este instante.**

El sistema debe detectar aceleraciones de precio y volumen mientras están ocurriendo, sin esperar necesariamente al cierre de la vela de 1 minuto.

La primera versión no utilizará noticias ni análisis fundamental en tiempo real.

---

# 2. Concepto original

La idea parte de los siguientes indicadores de alta demanda y baja oferta:

1. Demanda: subida diaria significativa.
2. Demanda: volumen relativo muy superior al normal.
3. Demanda: movimiento de precio acompañado de actividad extraordinaria.
4. Demanda: rango de precio negociable.
5. Oferta: pocas unidades disponibles.

Adaptación a criptomonedas:

| Factor | Condición inicial |
|---|---:|
| Precio | $2–$20 |
| Cambio 24h | ≥ +10% |
| RVOL | ≥ 5x |
| Circulating Supply | < 20M |
| Volumen 24h | ≥ $5M |

Estos parámetros serán configurables.

---

# 3. Por qué usar Bitget Spot

La primera versión utilizará **Bitget Spot** como universo de mercado.

La razón es mantener el sistema sencillo y evitar mezclar inicialmente:

- apalancamiento,
- funding,
- liquidaciones,
- open interest,
- estructura de futuros.

La arquitectura se dejará preparada para añadir Bitget USDT Futures posteriormente.

Bitget proporciona WebSocket público para datos de mercado en tiempo real y REST para histórico, bootstrap y recuperación de datos.

## Fuentes de datos

### WebSocket

Principalmente:

- ticker
- candle 1m
- candle 5m
- trades

### REST

Para:

- cargar instrumentos
- obtener histórico inicial
- reconstruir buffers después de una desconexión
- recuperar datos perdidos

**Principio:** WebSocket para tiempo real; REST para bootstrap/recovery.

---

# 4. Universo de criptomonedas

No es necesario analizar intensivamente todas las monedas de Bitget.

Primero se crea el universo:

```text
Bitget Spot USDT pairs
        ↓
Precio $2–$20
        ↓
Volumen 24h ≥ $5M
        ↓
Circulating Supply < 20M
        ↓
Candidatas
```

Sólo las candidatas pasan al análisis intensivo.

Esto reduce consumo de recursos y cantidad de suscripciones WebSocket.

---

# 5. Circulating Supply

El precio, volumen, trades y velas proceden de Bitget.

El **circulating supply** se almacenará en una fuente fundamental externa y se mantendrá en caché.

No es necesario consultar el supply continuamente.

Estructura propuesta:

```text
supply_cache

symbol
circulating_supply
market_cap
fdv
last_updated
```

El supply puede actualizarse periódicamente.

---

# 6. Motor de velas

El sistema mantendrá buffers de velas de 1 minuto.

Ejemplo:

```text
ABCUSDT

1m candles
├── 10:01
├── 10:02
├── 10:03
├── ...
└── 10:30
```

El histórico se utilizará para construir las referencias de volumen necesarias para RVOL.

Se recomienda conservar suficiente histórico para comparar cada franja horaria con sesiones anteriores.

---

# 7. Timeframes

El timeframe principal será:

**1 minuto**

También se calcularán:

- 3m
- 5m
- 15m
- 30m
- 1h
- 24h

El 1m se utiliza para detectar explosiones tempranas.

El 5m y 15m sirven para confirmar que el movimiento tiene continuidad.

El 1h y 24h proporcionan contexto.

---

# 8. RVOL — Relative Volume

RVOL será uno de los componentes principales del sistema.

No se utilizará simplemente:

```text
volumen actual / volumen medio diario
```

La comparación será intradía.

Por ejemplo, una vela de las 14:37 se compara con las velas históricas correspondientes a esa misma franja horaria.

Conceptualmente:

```text
RVOL =
volumen actual
────────────────────────
volumen histórico normal
```

Ejemplo:

```text
Volumen actual 1m = 850.000
Volumen normal    = 170.000

RVOL = 850.000 / 170.000
     = 5.0x
```

Un RVOL de 5x significa que el volumen es aproximadamente cinco veces el comportamiento normal de referencia.

---

# 9. Tres tipos de RVOL

El scanner tendrá tres métricas diferentes.

## 9.1 RVOL 1m

Detecta explosiones inmediatas.

```text
RVOL_1m =
volumen de la vela actual
/
volumen histórico normal de esa franja
```

Filtro inicial:

```text
RVOL_1m ≥ 5x
```

---

## 9.2 RVOL 5m

Mide el volumen agregado durante los últimos cinco minutos.

```text
RVOL_5m =
volumen últimos 5m
/
volumen histórico normal de ese período
```

Filtro inicial:

```text
RVOL_5m ≥ 3x
```

Sirve para evitar que una única operación o vela produzca una falsa señal.

---

## 9.3 RVOL de sesión

Mide el volumen acumulado durante el día.

```text
RVOL_session =
volumen acumulado actual
/
volumen histórico acumulado equivalente
```

Sirve para determinar si el día completo está siendo extraordinariamente activo.

---

# 10. Mediana frente a promedio

La referencia de volumen debería utilizar preferentemente la **mediana**.

Ejemplo:

```text
100
110
95
105
98
120
103
5800
102
99
```

El valor 5800 distorsiona fuertemente el promedio.

La mediana es mucho más robusta.

También se pueden almacenar percentiles:

```text
P50 = volumen normal
P75 = volumen elevado
P90 = volumen excepcional
P95 = volumen extremo
```

Esto permite clasificar mejor los movimientos anormales.

---

# 11. Aceleración de demanda

No basta con que RVOL sea alto.

Queremos saber si el volumen está aumentando.

Ejemplo:

```text
4.1x
 ↓
4.8x
 ↓
6.2x
 ↓
8.7x
```

Esto indica expansión de demanda.

En cambio:

```text
8.1x
8.0x
7.9x
7.8x
```

indica que el volumen sigue siendo alto, pero está perdiendo intensidad.

---

# 12. Demand Burst

Se creará una métrica denominada:

**Demand Burst**

Conceptualmente:

```text
Demand Burst =
RVOL_1m actual
/
RVOL_1m de hace 5 minutos
```

Ejemplo:

```text
RVOL actual = 8.4x
RVOL hace 5m = 3.1x

Demand Burst = 2.71
```

Esto indica una fuerte aceleración de la actividad.

Un valor inferior a 1 indica pérdida de intensidad.

---

# 13. Momentum

El sistema calculará retornos en diferentes horizontes:

```text
return_1m
return_3m
return_5m
return_15m
return_30m
return_1h
return_24h
```

Ejemplo de momentum fuerte:

```text
1m       +0.82%
3m       +1.91%
5m       +3.12%
15m      +4.87%
30m      +6.22%
1h       +7.04%
24h     +12.31%
```

Esto indica que el movimiento está acelerando.

Una moneda con +18% en 24h pero con momentum negativo en 1m/5m/15m puede estar perdiendo fuerza.

---

# 14. Price Acceleration

También se calculará la aceleración del precio.

Se puede utilizar un z-score del retorno:

```text
z_return =
(retorno actual - media de retornos)
/
desviación estándar de retornos
```

Un valor elevado indica un movimiento estadísticamente extraordinario.

Como punto de partida:

```text
z_return > 3
```

puede considerarse un movimiento excepcional, aunque este umbral deberá calibrarse con datos reales.

---

# 15. VWAP

Se calculará VWAP intradía.

Fórmula:

```text
Typical Price =
(H + L + C) / 3
```

y:

```text
VWAP =
Σ(Typical Price × Volume)
/
Σ Volume
```

Condición básica:

```text
Price > VWAP
```

También se calculará la distancia:

```text
distance_from_vwap =
(price / VWAP - 1) × 100
```

Ejemplo:

```text
Price = $4.82
VWAP  = $4.63

Distance = +4.10%
```

---

# 16. Penalización por extensión

No queremos que el scanner persiga automáticamente una moneda demasiado extendida.

Por ejemplo:

```text
Distance from VWAP > 10%
```

→ penalización.

```text
Distance from VWAP > 15%
```

→ penalización fuerte.

La moneda puede seguir subiendo, pero deja de considerarse una entrada de momentum temprana.

---

# 17. Price Burst

También se puede medir la aceleración del precio.

Se utilizará inicialmente una medida estadística como z-score del retorno.

Esto permite distinguir:

```text
movimiento normal
```

de:

```text
movimiento extraordinario
```

y alimentar el score final.

---

# 18. Momentum Score

El sistema tendrá un score de 0 a 100.

Propuesta inicial:

```text
24h momentum              0–10
1h momentum               0–10
15m momentum              0–10
5m momentum               0–15
3m momentum               0–10
1m momentum               0–5

RVOL 1m                   0–15
RVOL 5m                   0–10
RVOL acceleration         0–5

VWAP                      0–5
Price acceleration        0–5

Supply                    0–5
────────────────────────────
TOTAL                    100
```

Estos pesos son iniciales y posteriormente deberán calibrarse mediante backtesting.

---

# 19. Estructura del Signal Score

El score final se puede conceptualizar como:

```text
                         SCORE
                           │
        ┌──────────────────┼─────────────────┐
        │                  │                 │
     MOMENTUM            DEMAND           STRUCTURE
        │                  │                 │
      40 pts             40 pts            20 pts
        │                  │                 │
    1m/5m/15m           RVOL/Burst        VWAP
    acceleration        volume            trend
        │                  │                 │
        └──────────────────┼─────────────────┘
                           ▼
                       0–100
```

Interpretación:

```text
0–49       NORMAL
50–64      WATCH
65–79      HOT
80–89      SIGNAL
90–100     EXTREME
```

Las alertas se generarían inicialmente sólo a partir de 80.

---

# 20. Early Signal

Una característica importante será no esperar obligatoriamente al +10% diario.

Se puede generar una señal temprana cuando:

```text
24h change > +5%

AND

RVOL_1m > 5

AND

RVOL_5m > 3

AND

5m return > 1.5%

AND

price > VWAP

AND

Demand Burst > 1.5
```

Ejemplo:

```text
24h       +6.4%
RVOL      7.2x
5m        +2.8%
VWAP      +1.7%
Burst     2.1x

→ EARLY SIGNAL
```

Si posteriormente alcanza el +10%, se puede convertir en:

```text
CONFIRMED SIGNAL
```

Esto permite detectar el movimiento antes de que cumpla el criterio original de +10%.

---

# 21. Estados del scanner

Cada activo puede encontrarse en uno de estos estados:

```text
NORMAL
   ↓
WATCH
   ↓
HOT
   ↓
SIGNAL
   ↓
EXTREME
```

Propuesta inicial:

### WATCH

```text
Score > 60
```

### HOT

```text
Score > 75
RVOL > 3x
```

### SIGNAL

```text
Score > 85
RVOL > 5x
Price > VWAP
Momentum 5m positivo
```

### EXTREME

```text
Score > 95
RVOL > 10x
Aceleración extrema
```

Los umbrales serán configurables y se calibrarán posteriormente.

---

# 22. Buy Pressure

Como fuente adicional de información, se utilizará el canal de trades de Bitget.

Esto permite estudiar:

- volumen comprador
- volumen vendedor
- número de trades
- tamaño medio de operación
- velocidad de trades
- presión compradora

Una métrica posible:

```text
Buy Pressure =
volumen de compras agresivas
/
volumen total negociado
```

Ejemplo:

```text
Buy Pressure = 68%
RVOL          = 7x
Price          ↑
Acceleration   ↑
```

La combinación es una confirmación adicional de demanda.

Esta métrica no será necesaria para V1, pero queda preparada para V2.

---

# 23. Order Book — V2

En una segunda versión se añadirá el order book.

Posibles métricas:

- bid/ask imbalance
- spread
- profundidad
- absorción
- retirada de asks
- agresividad compradora

Una métrica básica:

```text
Order Book Imbalance =
bid liquidity
/
(bid liquidity + ask liquidity)
```

No se incluirá inicialmente para evitar complicar el sistema antes de validar RVOL y momentum.

---

# 24. Arquitectura técnica

La arquitectura propuesta:

```text
                 REAL-TIME DATA
                      │
                      ▼
               ┌─────────────┐
               │ PRICE FEED  │
               └──────┬──────┘
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
      Price         Volume        Trades
        │             │             │
        └─────────────┼─────────────┘
                      ▼
              ┌──────────────┐
              │ CANDLE ENGINE│
              └──────┬───────┘
                     │
        ┌────────────┼─────────────┐
        ▼            ▼             ▼
     Momentum       RVOL          VWAP
        │            │             │
        └────────────┼─────────────┘
                     ▼
               FILTER ENGINE
                     │
          ┌──────────┴──────────┐
          ▼                     ▼
       Supply                 Market Cap
          │                     │
          └──────────┬──────────┘
                     ▼
                SCORE 0–100
                     │
          ┌──────────┴──────────┐
          ▼                     ▼
       WATCHLIST               ALERT
                                 │
                                 ▼
                            Dashboard
```

---

# 25. Arquitectura de software Python

Propuesta:

```text
Python
│
├── bitget_client/
│   ├── websocket.py
│   ├── rest.py
│   └── subscriptions.py
│
├── market_data/
│   ├── candles.py
│   ├── trades.py
│   └── instruments.py
│
├── indicators/
│   ├── rvol.py
│   ├── momentum.py
│   ├── vwap.py
│   └── acceleration.py
│
├── scanner/
│   ├── universe.py
│   ├── filters.py
│   ├── score.py
│   └── signals.py
│
├── storage/
│   ├── redis.py
│   └── postgres.py
│
├── api/
│   └── server.py
│
└── dashboard/
    └── frontend
```

---

# 26. Redis

Redis se utilizará para datos de estado rápido:

- precio actual
- velas recientes
- RVOL
- momentum
- VWAP
- scores
- ranking
- estado de cada señal

El objetivo es que el dashboard pueda consultar el estado actual sin realizar cálculos pesados.

---

# 27. PostgreSQL

PostgreSQL almacenará datos persistentes:

- histórico
- señales
- scores
- métricas al producirse la señal
- resultado posterior de cada señal

Ejemplo:

```text
timestamp
symbol
price
score
rvol_1m
rvol_5m
rvol_session
demand_burst
momentum_1m
momentum_5m
momentum_15m
vwap
vwap_distance
supply
volume_24h
signal_type
```

---

# 28. Registro para backtesting

Cada señal debe quedar almacenada.

Después podremos responder:

```text
¿Qué ocurrió 1 minuto después?
¿Qué ocurrió 5 minutos después?
¿Qué ocurrió 15 minutos después?
¿Qué ocurrió 30 minutos después?
¿Qué ocurrió 1 hora después?
```

También podemos medir:

```text
maximum favorable excursion
maximum adverse excursion
```

y determinar qué scores tienen realmente mayor expectativa.

Esto es fundamental para evitar construir un sistema basado únicamente en intuiciones.

---

# 29. WebSocket Event Engine

Flujo:

```text
Bitget WebSocket
       │
       ▼
   new candle/update
       │
       ▼
Update 1m buffer
       │
       ├── update VWAP
       ├── update RVOL
       ├── update momentum
       ├── update acceleration
       └── update score
                    │
                    ▼
              SIGNAL ENGINE
                    │
             ┌──────┴──────┐
             ▼             ▼
          Dashboard      Alert
```

Como las velas pueden recibir actualizaciones antes del cierre, el sistema puede recalcular las métricas mientras la vela está formándose.

---

# 30. Dashboard

La pantalla principal debe ser sencilla y orientada a decisión.

Ejemplo:

```text
┌─────────────────────────────────────────────────────────────────┐
│                  BITGET REAL-TIME SCANNER                       │
├────┬─────────┬───────┬──────┬───────┬───────┬───────┬─────────┤
│ #  │ SYMBOL  │ PRICE │ 24H  │ 5M    │ RVOL  │ BURST │ SCORE   │
├────┼─────────┼───────┼──────┼───────┼───────┼───────┼─────────┤
│ 1  │ XYZ     │ 6.72  │+13.8%│+3.7%  │ 7.8x  │ 2.4x  │ 89      │
│ 2  │ ABC     │ 4.31  │+11.2%│+2.9%  │ 6.4x  │ 1.9x  │ 84      │
│ 3  │ DEF     │ 9.18  │+17.3%│+1.2%  │ 5.8x  │ 1.1x  │ 76      │
│ 4  │ QWE     │ 3.84  │ +7.2%│+2.8%  │ 8.9x  │ 3.2x  │ 82      │
└────┴─────────┴───────┴──────┴───────┴───────┴───────┴─────────┘
```

La tabla se ordenará por **Score**, no por cambio porcentual.

---

# 31. Ejemplo de señal

Supongamos:

```text
XYZUSDT

Price              $6.72
24h                 +13.8%
5m                   +3.7%
15m                  +5.1%
1h                    +7.2%

RVOL 1m              7.8x
RVOL 5m              5.1x
RVOL session         3.9x

Demand Burst         2.4x

VWAP                 $6.43
Distance VWAP        +4.5%

Supply               11.8M
24h Volume           $32.4M
```

Resultado hipotético:

```text
MOMENTUM        35/40
DEMAND          37/40
STRUCTURE       17/20

TOTAL           89/100

SIGNAL
```

Los pesos y resultados son ejemplos de diseño, no una garantía de rentabilidad.

---

# 32. Parámetros iniciales V1

```text
Exchange:               Bitget
Market:                 Spot
Quote:                  USDT

Precio:                 $2 – $20
Cambio 24h:             ≥ +10%
Circulating supply:     < 20M
Volumen 24h:            ≥ $5M

RVOL 1m:                ≥ 5x
RVOL 5m:                ≥ 3x

Precio vs VWAP:         > VWAP

Timeframe principal:    1m

Momentum:               1m / 3m / 5m / 15m / 30m / 1h / 24h

Early Signal:
  24h > +5%
  RVOL 1m > 5x
  RVOL 5m > 3x
  5m return > 1.5%
  price > VWAP
  Demand Burst > 1.5

Signal Score:           ≥ 80
```

Todos los parámetros deben ser configurables.

---

# 33. Roadmap

## V1 — Scanner básico

```text
Bitget Spot
↓
USDT pairs
↓
Price filter
↓
Volume filter
↓
Supply filter
↓
1m candles
↓
RVOL
↓
Momentum
↓
VWAP
↓
Acceleration
↓
Score 0–100
↓
Dashboard
```

## V2 — Microestructura

Añadir:

```text
Trades
↓
Buy/Sell pressure
↓
Trade velocity
↓
Order book
↓
Spread
↓
Depth imbalance
```

## V3 — Backtesting

Guardar todas las señales y estudiar sus resultados posteriores.

Objetivo:

```text
Score
↓
¿qué probabilidad existe de +3%, +5%, +10%?
↓
¿en cuánto tiempo?
↓
¿cuál es el drawdown antes de alcanzar el máximo?
```

A partir de esos resultados se podrán ajustar los pesos del score de forma objetiva.

---

# 34. Principio fundamental del proyecto

El scanner no debe intentar responder:

> "¿Qué criptomoneda está subiendo?"

Debe responder:

> **"¿Qué criptomoneda está experimentando ahora mismo una expansión estadísticamente anormal de precio y volumen, y qué tan fuerte es esa expansión?"**

La combinación central será:

```text
MOMENTUM
    +
RVOL
    +
DEMAND BURST
    +
PRICE ACCELERATION
    +
VWAP STRUCTURE
    +
SUPPLY
```

La señal más interesante será aquella donde **precio y volumen se aceleren simultáneamente**, mientras la moneda todavía no esté excesivamente extendida respecto al VWAP.

---

# 35. Arquitectura final resumida

```text
                     BITGET
                       │
              WebSocket + REST
                       │
                       ▼
                MARKET DATA
                       │
        ┌──────────────┼──────────────┐
        │              │              │
      PRICE          VOLUME         TRADES
        │              │              │
        └──────────────┼──────────────┘
                       ▼
                 1M CANDLE ENGINE
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
      MOMENTUM         RVOL           VWAP
        │              │              │
        └──────────────┼──────────────┘
                       ▼
              ACCELERATION ENGINE
                       │
                       ▼
                DEMAND BURST
                       │
                       ▼
                FILTER ENGINE
                       │
             ┌─────────┴─────────┐
             ▼                   ▼
          SUPPLY              MARKET CAP
             │                   │
             └─────────┬─────────┘
                       ▼
                 SCORE ENGINE
                       │
                ┌──────┴──────┐
                ▼             ▼
             WATCHLIST       SIGNAL
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
                DASHBOARD             ALERT
                    │
                    ▼
                DATABASE
                    │
                    ▼
                BACKTESTING
                    │
                    ▼
             SCORE CALIBRATION
```

## Siguiente fase recomendada

La implementación debería empezar por **V1**, sin trading automático:

1. Conectar Bitget WebSocket.
2. Obtener el universo de pares USDT.
3. Filtrar candidatos.
4. Construir el buffer de velas 1m.
5. Implementar RVOL 1m/5m/session.
6. Implementar momentum.
7. Implementar VWAP.
8. Implementar Demand Burst.
9. Implementar Score 0–100.
10. Crear dashboard en tiempo real.
11. Guardar cada señal en PostgreSQL.
12. Después de acumular suficientes datos, realizar backtesting y recalibrar los pesos.

La prioridad es **medir primero y optimizar después**. El scanner debe demostrar estadísticamente qué combinaciones de variables anticipan movimientos posteriores antes de utilizarse como herramienta de ejecución.
