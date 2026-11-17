#!/bin/sh
# Sobe a bridge do QR Code (opcional) e o servidor. Dados persistentes ficam em $DATA_DIR (disco da hospedagem).
DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR/auth" "$DATA_DIR/uploads"
export DATABASE_PATH="${DATABASE_PATH:-$DATA_DIR/sdr.db}" UPLOAD_DIR="${UPLOAD_DIR:-$DATA_DIR/uploads}"
if [ "${QR_ENABLED:-1}" = "1" ] && [ -f bridge/server.js ]; then
  ( cd bridge && while true; do
      APP_URL="http://127.0.0.1:${PORT:-8080}" BRIDGE_AUTH_DIR="$DATA_DIR/auth" node server.js
      echo "bridge parou; reiniciando em 5s" ; sleep 5
    done ) &
fi
exec python3 run.py
