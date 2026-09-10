#!/usr/bin/env bash
# Despliegue del scanner en el VPS. Se ejecuta EN EL VPS, no en local.
#
#   ssh deploy@VPS_HOST 'bash /opt/scanner_volumen/deploy/desplegar.sh'
#
# Hace, en este orden: copia de seguridad de la base, comprobacion de que el
# arbol esta limpio, `git pull --ff-only`, reinicio del servicio y
# verificacion de que ha arrancado de verdad. Si algo falla, se para y lo
# dice: nunca deja el servicio a medias en silencio.
#
# Es idempotente: si no hay nada nuevo que traer, lo dice y no reinicia.
set -euo pipefail

RAIZ="${RAIZ:-/opt/scanner_volumen}"
SERVICIO="${SERVICIO:-scanner}"
RAMA="${RAMA:-master}"
BASE="$RAIZ/data/scanner.db"

cd "$RAIZ"
echo "== despliegue de $RAMA en $(hostname) =="

# 1. ¿Hay algo que traer? Si no, no se toca el servicio: un reinicio
#    innecesario corta el WebSocket y obliga a rellenar velas otra vez.
git fetch --quiet origin "$RAMA"
ANTES="$(git rev-parse HEAD)"
DESTINO="$(git rev-parse "origin/$RAMA")"
if [ "$ANTES" = "$DESTINO" ]; then
  echo "   ya esta en $(git log --oneline -1). Nada que hacer."
  exit 0
fi
echo "   $(git log --oneline -1 HEAD)  ->  $(git log --oneline -1 "origin/$RAMA")"

# 2. El arbol tiene que estar limpio. Un fichero modificado a mano aqui hace
#    que `git pull` se niegue a mitad -y ya paso una vez con deploy/setup.sh.
#    No se descarta nada automaticamente: borrar cambios de produccion sin
#    que nadie los mire es peor que parar.
if [ -n "$(git status --porcelain)" ]; then
  echo "   ABORTADO: hay cambios sin commitear en el VPS:"
  git status --porcelain | sed 's/^/     /'
  echo "   Revisalos y, si sobran:  git checkout -- <fichero>"
  exit 1
fi

# 3. Copia de la base ANTES de nada. El despliegue puede traer migraciones de
#    esquema, y esas no se deshacen con un `git checkout`.
if [ -f "$BASE" ]; then
  COPIA="$BASE.bak-$(date +%Y%m%d-%H%M%S)"
  cp "$BASE" "$COPIA"
  echo "   copia de seguridad: $COPIA ($(du -h "$COPIA" | cut -f1))"
fi

# 4. Traer y reiniciar.
git pull --ff-only --quiet origin "$RAMA"
echo "   ahora en: $(git log --oneline -1)"
sudo systemctl restart "$SERVICIO"

# 5. Verificar que ha arrancado DE VERDAD. `systemctl restart` devuelve 0
#    aunque el proceso muera a los dos segundos, asi que no basta con eso.
echo "   esperando a que arranque..."
sleep 20
if ! systemctl is-active --quiet "$SERVICIO"; then
  echo "   FALLO: el servicio no esta activo. Ultimas lineas:"
  journalctl -u "$SERVICIO" -n 25 --no-pager | sed 's/^/     /'
  echo
  echo "   Para volver atras:"
  echo "     cd $RAIZ && git reset --hard $ANTES && sudo systemctl restart $SERVICIO"
  echo "   Y si el fallo es de esquema, restaura tambien la copia de arriba."
  exit 1
fi

# Los 429 al arrancar son el relleno de velas topando con el limite de
# peticiones: son ESPERADOS tras un reinicio y no indican nada roto.
ERRORES="$(journalctl -u "$SERVICIO" --since '2 min ago' --no-pager 2>/dev/null \
           | grep -iE 'ERROR|Traceback' | grep -v '429' | head -5 || true)"
BOT="$(journalctl -u "$SERVICIO" --since '2 min ago' --no-pager 2>/dev/null \
       | grep -iE 'bot ACTIVO|bot desactivado' | tail -1 || true)"

echo "   servicio: activo"
[ -n "$BOT" ] && echo "   ${BOT##*scanner: }"
if [ -n "$ERRORES" ]; then
  echo "   AVISO: hay errores en el log (revisalos, el servicio sigue en pie):"
  echo "$ERRORES" | sed 's/^/     /'
  exit 2
fi
echo "== listo =="
