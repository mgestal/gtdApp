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

echo "Backup creado: $ARCHIVE_PATH"