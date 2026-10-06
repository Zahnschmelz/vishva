#!/usr/bin/env bash
set -e

# Ins Vishva-Projekt-Root wechseln (dort wo das Skript liegt)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# venv aktivieren
VENV_PATH="./venv"
if [ -d "$VENV_PATH" ]; then
    echo "🐍 Aktiviere venv: $VENV_PATH"
    # shellcheck disable=SC1091
    source "$VENV_PATH/bin/activate"
else
    echo "⚠️ Kein venv unter $VENV_PATH — versuche System-Python"
fi

exec python3 -m vishva.test_tool "$@"
