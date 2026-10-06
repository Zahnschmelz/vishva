#!/usr/bin/env bash
# Aktiviert das Root-venv und führt den Tool-Smoke-Test aus.
set -e
cd "$(dirname "$0")/.."          # -> Projekt-Root
source ./venv/bin/activate
python test/tools.py "$@"
