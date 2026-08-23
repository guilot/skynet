#!/usr/bin/env bash
# Prepara un VPS recién creado (Ubuntu 24.04) para correr el scanner.
#
# Ejecutar DENTRO del checkout del repo en el servidor (p. ej.
# /opt/scanner_volumen), como el usuario con sudo que va a ser dueño del
# proceso. Es idempotente: se puede relanzar tras un git pull sin daño.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(whoami)"
SERVICE_NAME="scanner"

echo "== scanner: instalando en $APP_DIR (usuario $RUN_USER) =="

# 1) Dependencias de sistema. Ubuntu 24.04 trae Python 3.12 en los repos,
#    que es el mínimo que exige pyproject.toml.
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip git >/dev/null

python3 - <<'EOF'
import sys
if sys.version_info < (3, 12):
    sys.exit(f"ERROR: se necesita Python >= 3.12, hay {sys.version.split()[0]}")
print(f"Python {sys.version.split()[0]} OK")
EOF

# 2) Entorno virtual e instalación del paquete (editable, como en local).
if [ ! -d "$APP_DIR/.venv" ]; then
    python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR"

# 3) Unidad de systemd con las rutas de ESTE servidor sustituidas.
sed "s|__USER__|$RUN_USER|g; s|__APP_DIR__|$APP_DIR|g" \
    "$APP_DIR/deploy/scanner.service" \
    | sudo tee "/etc/systemd/system/$SERVICE_NAME.service" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"

sleep 2
systemctl --no-pager --lines=5 status "$SERVICE_NAME" || true

echo
echo "Hecho. Sigue los logs con:  journalctl -u $SERVICE_NAME -f"
echo "Dashboard (desde tu PC):    ssh -L 8000:127.0.0.1:8000 $RUN_USER@SERVIDOR"
