#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "🔧 Erstelle virtuelle Umgebung..."
    python3 -m venv "$VENV_DIR"
fi

echo "🚀 Aktiviere venv..."
source "$VENV_DIR/bin/activate"

if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=0
    export PLAYWRIGHT_BROWSERS_PATH=0
    echo "📦 Installiere Abhängigkeiten..."
    pip install --quiet -r "$SCRIPT_DIR/requirements.txt"
    playwright install chromium > /dev/null 2>&1
fi

echo "🤖 Starte Agent..."
cd "$SCRIPT_DIR"
python -m vishva.agent "$@"
