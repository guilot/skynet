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
4. **Cuando el stop salta en el exchange, el bot ya sabe leer ese cierre.**
   Lo hace correlacionando por identificador, no por ventana de tiempo: al
   ejecutarse un plan order, Bitget crea una orden cuyo `clientOid` es el
   `orderId` del propio plan order -o sea, el `stop_id` que el bot tiene
   guardado- y cuyo `orderSource` es `loss_market`. Verificado contra la
   cuenta de simulación con un stop disparado de verdad.

   Sigue habiendo un camino degradado, y conviene conocerlo: si la posición
   no tiene `stop_id` (una degradada a la que nunca se le pudo colocar el
   stop) o la orden no aparece en la ventana consultada, el bot **no
   inventa un precio**: deja la fila intacta, la marca `degradada` y la
   cuenta en la línea `Cierres SIN fill real` del informe. Esa línea debería
   ser normalmente cero; si sube, hay filas que revisar a mano, y cada una
   ocupa un hueco de concurrencia.

5. **Una orden mandada por un proceso que muere antes de registrarla.**
   Puede pasar bajo `Restart=always`: la orden llega a Bitget pero el bot no
   la registra. El endpoint de posiciones no devuelve el identificador de
   cliente, así que por esa vía el bot no la reconoce como suya -pero el
   historial de órdenes SÍ lo devuelve, así que la busca ahí por su
   identificador y, si aparece ejecutada, **la adopta con sus datos reales**
   y pasa a gestionarla como cualquier otra (le colocará su stop). Se ve en
   el log como `reserva adoptada por historial`.

   Si tampoco aparece en el historial, no se puede afirmar que se ejecutara:
   la fila se deja intacta y el símbolo queda **vetado** el resto de la
   sesión, contado como `reserva sin correlacionar`. Ese caso sí **requiere
   mirar Bitget a mano**: puede haber ahí una posición real apalancada y sin
   stop, porque el stop se coloca después de confirmar la apertura; o se le
   pone uno, o se cierra.
