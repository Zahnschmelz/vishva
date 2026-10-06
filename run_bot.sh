#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

mkdir -p "$SCRIPT_DIR/tmp"

if [ ! -x "$VENV_DIR/bin/python3" ]; then
    echo "🔧 Erstelle/repariere virtuelle Umgebung..."
    rm -rf "$VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=0
    export PLAYWRIGHT_BROWSERS_PATH=0
    echo "📦 Installiere Abhängigkeiten..."
    "$VENV_DIR/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
    playwright install chromium > /dev/null 2>&1
fi

echo "🤖 Starte Telegram Bot..."
cd "$SCRIPT_DIR"
exec "$VENV_DIR/bin/python" -m vishva.bot
