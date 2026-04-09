#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="$(basename -- "$SCRIPT_DIR")"
PARENT_DIR="$(dirname -- "$SCRIPT_DIR")"
BACKUP_DIR="$SCRIPT_DIR/backups"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
ARCHIVE_NAME="${APP_NAME}_backup_${TIMESTAMP}.tgz"
ARCHIVE_PATH="$BACKUP_DIR/$ARCHIVE_NAME"
SUDO_CMD=()
TARGET_OWNER="mgestal"
TARGET_GROUP="www-data"

mkdir -p "$BACKUP_DIR"

if [[ ! -w "$BACKUP_DIR" ]]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO_CMD=(sudo)
  else
    echo "Error: no se puede escribir en $BACKUP_DIR y sudo no está disponible." >&2
    exit 1
  fi
fi

"${SUDO_CMD[@]}" tar \
  --exclude="${APP_NAME}/backups/*.tgz" \
  -czf "$ARCHIVE_PATH" \
  -C "$PARENT_DIR" \
  "$APP_NAME"

if [[ "$(id -u)" -eq 0 ]]; then
  chown "${TARGET_OWNER}:${TARGET_GROUP}" "$ARCHIVE_PATH"
elif command -v sudo >/dev/null 2>&1; then
  sudo chown "${TARGET_OWNER}:${TARGET_GROUP}" "$ARCHIVE_PATH"
  sudo chmod 775 "$ARCHIVE_PATH"
else
  echo "Aviso: no se pudo ajustar propietario a ${TARGET_OWNER}:${TARGET_GROUP} (sudo no disponible)." >&2
fi

echo "Backup creado: $ARCHIVE_PATH"
