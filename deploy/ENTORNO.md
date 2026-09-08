# El fichero de entorno del servicio: `/etc/scanner_volumen.env`

Todo lo que es un secreto -y el interruptor del dinero real- vive **fuera del
repositorio**, en un único fichero que solo lee el servicio de systemd.

**Nunca** en el repositorio: quedaría en el histórico de git para siempre, se
copiaría a cada clon y viajaría en cada `git pull`.

**Nunca** dentro del fichero de la unidad (`scanner.service`): `systemctl cat
scanner` lo imprime entero, y lo imprime para cualquiera que pueda ejecutarlo.
Por eso la unidad solo lleva una referencia:

```ini
EnvironmentFile=-/etc/scanner_volumen.env
```

El guion inicial (`-`) hace que un fichero ausente no impida arrancar: en modo
`paper` -el que corre hoy en producción- no hace falta ninguna credencial, y
el servicio debe seguir levantándose en una máquina que nunca las ha tenido.
Si el fichero falta cuando sí hacen falta, el bot no arranca y lo dice: la
factoría del arranque (`scanner_volumen/__main__.py`) nombra las variables que
echa en falta -nunca su contenido.

## Crearlo

```bash
sudo install -m 600 -o root -g root /dev/null /etc/scanner_volumen.env
sudo -e /etc/scanner_volumen.env       # o el editor que prefieras
sudo systemctl restart scanner
```

Los permisos **600** (solo root lee y escribe) no son un detalle: son la razón
por la que este fichero es un sitio aceptable para una clave de exchange.
Compruébalos con `ls -l /etc/scanner_volumen.env`; deben salir `-rw-------`.

Systemd relee el fichero en cada arranque del servicio, así que cualquier
cambio necesita un `systemctl restart scanner` -no basta con `daemon-reload`.

## Qué lleva

```sh
# --- El interruptor del dinero real (la SEGUNDA llave) ---
# La primera es `modo = "real"` en config.toml. Hacen falta las dos, y esta
# vive fuera del repositorio a propósito: un `git pull` que traiga por error
# una config con "real" no puede, por sí solo, poner dinero en juego.
#   sin definir  -> con modo="real", el proceso NO arranca (y dice por qué)
#   lectura      -> se conecta a Bitget de verdad, pero ejecuta en paper:
#                   NO manda ni una orden. El escalón intermedio.
#   ordenes      -> manda órdenes reales. Aquí se mueve dinero.
SCANNER_BOT_REAL=lectura

# --- Credenciales de la subcuenta de PRODUCCIÓN ---
# De una SUBCUENTA dedicada, con su propio saldo: un fallo grave o unas claves
# filtradas no deben poder tocar el resto del capital. Solo se leen en los
# modos reales; en `paper` el proceso ni las mira.
SCANNER_BITGET_KEY=...
SCANNER_BITGET_SECRET=...
SCANNER_BITGET_PASSPHRASE=...

# --- Credenciales de la cuenta de DEMO (simulación) ---
# Las usa únicamente el banco de pruebas de integración
# (`tests/integracion_bitget/`), que se ejecuta a mano y se salta solo si
# faltan. El proceso del scanner NUNCA las lee.
SCANNER_BITGET_DEMO_KEY=...
SCANNER_BITGET_DEMO_SECRET=...
SCANNER_BITGET_DEMO_PASSPHRASE=...
```

Los nombres de producción y de demo son deliberadamente distintos, no dos
variantes de lo mismo: si se llamaran igual en dos ficheros, un copiar y pegar
descuidado acabaría operando la cuenta real con las claves de prueba, o al
revés.

## Comprobaciones antes de poner `SCANNER_BOT_REAL=ordenes`

1. Los diez supuestos sobre la API de Bitget que la Task 12 dejó **sin
   confirmar** (endpoints, nombres de campo, forma de las respuestas) siguen
   sin verificar mientras no se ejecute el banco de integración con claves de
   demo. Hasta entonces, el modo real es código probado contra un exchange
   imaginario.
2. En Bitget, cada símbolo que se vaya a operar debe estar en **margen
   aislado** y al apalancamiento que asume la estrategia. El bot lo comprueba
   por símbolo antes de su primera entrada y **veta** el que no coincida: nunca
   cambia la configuración de la cuenta por su cuenta.
3. El freno de emergencia se acciona creando el fichero indicado por
   `bot.fichero_parada` en `config.toml` (por defecto `data/parar_bot`, relativo
   al `WorkingDirectory` del servicio). Con él presente no se abren entradas
   nuevas; las posiciones abiertas se siguen gobernando. Se quita borrándolo.
   Ninguna de las dos cosas exige reiniciar el proceso, y funciona en **todos**
   los modos, `paper` incluido.
4. **Hoy el bot no sabe recuperar el precio de un cierre que ejecutó el
   exchange.** Es la limitación más importante de este despliegue y la salida
   por stop es la salida NORMAL de la estrategia, así que va a pasar:

   - Qué ocurre: el sondeo detecta que la posición ya no está en Bitget, pero
     no puede conseguir el fill real de ese cierre (haría falta una consulta
     al historial de fills por símbolo que el cliente aún no tiene). Nunca
     inventa un precio: deja la fila abierta, la marca `degradada` y grita en
     el log.
   - Cómo se ve: en el informe, la línea `Cierres SIN fill real (posiciones
     varadas...)` y el contador de `(degradadas: N)` entre las abiertas.
   - Por qué importa: cada posición varada sigue ocupando un hueco de
     concurrencia. Con `max_concurrentes = 5`, cinco de ellas dejan al bot sin
     abrir nada (`descartes tope concurrencia`), y sobreviven al reinicio.
   - Qué hacer: cerrar esas filas a mano (o revisarlas) antes de que se
     acumulen. **Vigila esa línea del informe a diario mientras el modo
     `ordenes` esté encendido.**
5. **Una orden mandada por un proceso que muere antes de registrarla veta su
   símbolo.** El endpoint de posiciones de Bitget no devuelve el identificador
   de cliente de la orden, así que el bot no puede reconocer como propia esa
   posición. Hace lo conservador -no la toca y no abre nada más en ese
   símbolo durante la sesión-, pero esa posición real puede estar **apalancada
   y sin stop en el exchange**, porque el stop se coloca después de confirmar
   la apertura. Se ve en el informe como `reserva sin correlacionar` en el log
   y como un símbolo vetado. **Requiere mirar Bitget a mano**: o se le pone un
   stop, o se cierra.
