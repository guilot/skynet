# Criterio de decisión de la estrategia

**Escrito el 14 de septiembre de 2026, ANTES de ejecutar el backtest extendido
y antes de ver ningún resultado nuevo.** Ese orden es el punto de este
documento: fijar de antemano qué contaría como evidencia, para no decidirlo
después mirando los números.

## Por qué existe

Con los datos disponibles hoy no se puede saber si la estrategia tiene
ventaja:

- 99 trades en el backtest (30 ago – 14 sep), neto **+267,09 USDT**
- pero **una sola operación** (MARSCOINUSDT, +211,32) es el **79%** de ese neto
- quitando las **dos** mejores, el neto es **−0,17**: exactamente cero
- el bot en paper, 40 operaciones reales: **−29,39 USDT**, mediana −0,22

Durante la semana previa se formularon tres hipótesis a partir de casos
concretos llamativos —la salida tras EXTREME, el filtro de empuje en 1m, y el
tamaño según la calidad de la señal—. Las tres parecían sólidas y **las tres
se cayeron** al contrastarlas contra muestra partida o contra los datos
reales. Con 40-99 operaciones y una distribución dominada por un trade,
siempre se encuentra un patrón.

## El criterio

Sobre el backtest extendido (3 meses, ~600 trades esperados), la estrategia
se considera **con ventaja** si cumple **las tres**:

1. **Neto positivo excluyendo las 3 mejores operaciones.** Si el resultado
   depende de la cola extrema, no es una ventaja medible: es una apuesta a
   que la cola se repita.
2. **Neto positivo en al menos 2 de los 3 tercios** en que se parta la
   muestra por tiempo. Un solo tercio bueno que arrastre a los otros dos es
   el mismo problema del punto 1, desplazado.
3. **Neto positivo después de comisiones**, que ya están incluidas en el
   cálculo (a 20x y con salidas escalonadas suponen ~0,47 USDT por operación
   sobre ~20 de margen, y en los datos actuales son el 18% de la pérdida).

Si no se cumplen las tres: **la estrategia no ha demostrado ventaja**, y la
respuesta correcta es no operarla con dinero real, no buscar el parámetro que
la arregle.

## Lo que NO cuenta como evidencia

- El win rate por sí solo. Con salidas escalonadas se puede subir cobrando
  antes y ganar menos dinero.
- Un resultado bueno tras probar varias configuraciones. Cada barrido
  adicional sobre la misma muestra aumenta la probabilidad de encontrar un
  ganador por azar. Si se barre, se declara cuántas configuraciones se
  probaron.
- Un caso concreto, por llamativo que sea, mirado después de conocer su
  resultado.

## Sesgos conocidos del backtest extendido

Se anotan aquí para que el resultado se lea con ellos delante:

1. **Supervivencia**: el universo se reconstruye con los pares que existen
   hoy. Los que se listaron y murieron en el periodo no aparecen, y esta
   estrategia opera justo el tipo de alt-coin donde eso pasa. **Sesga al
   alza.**
2. **`market_cap` aproximado** con el suministro actual: es la única de las 13
   entradas del score que no se puede reconstruir desde las velas.
3. **El backtest es pesimista con los stops**: lee el máximo y el mínimo de
   cada vela de un minuto, mientras que el bot en vivo solo ve los precios que
   muestrea. Medido sobre las 39 operaciones que ambos tomaron: el bot salió
   **+32,22 USDT mejor** en las salidas por stop.

## Añadido (14 sep, antes de ejecutar): fidelidad de la reconstrucción

Al validar el reconstructor contra las transiciones que el escáner generó en
vivo sobre los mismos datos, aparece una cuarta limitación que no estaba
prevista:

4. **La reconstrucción produce el ~76% de las transiciones** que el escáner
   genera en vivo. El escáner evalúa cada segundo contra la vela en curso; el
   histórico de Bitget solo baja a la granularidad de un minuto, así que la
   reconstrucción evalúa una vez por vela cerrada. Las transiciones que faltan
   son picos intra-minuto.

   **No se sabe en qué dirección sesga**: no hay forma de saber si esas
   entradas perdidas habrían sido mejores o peores. Lo que sí se puede afirmar
   es que el backtest histórico es una versión **más gruesa** de la estrategia,
   no la misma.

   (Antes de enchufar `market_cap` con el suministro actual, la reconstrucción
   se quedaba en el 62% y el score medio salía 4 puntos bajo. Ese componente
   vale hasta 5 puntos y no era neutral omitirlo.)
