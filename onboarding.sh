#!/usr/bin/env bash
set -euo pipefail

# Ins Projekt-Root wechseln (dort wo das Skript liegt)
cd "$(dirname "$0")"

# venv aktivieren, falls vorhanden (laut config: working_dir/.venv)
VENV_PATH="venv"
if [ -d "$VENV_PATH" ]; then
    echo "🐍 Aktiviere venv: $VENV_PATH"
    # shellcheck disable=SC1091
    source "$VENV_PATH/bin/activate"
else
    echo "⚠️  Kein venv gefunden unter $VENV_PATH"
    echo "   Erstelle es mit: python3 -m venv $VENV_PATH && source $VENV_PATH/bin/activate && pip install -r requirements.txt"
    echo "   Oder setze auto_venv=false in config.json und installiere Dependencies global."
    exit 1
fi

# Onboarding starten
exec python3 vishva/onboarding.py "$@"
