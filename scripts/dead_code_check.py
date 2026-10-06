#!/usr/bin/env python3
"""
dead_code_check.py – findet ungenutzte Funktionen/Methoden ("Karteileichen").

Usage:
    python3 dead_code_check.py [root_dir]

Logik:
  1. Alle .py-Dateien per ast parsen → jede def (Funktion, Methode, nested).
  2. Projektweit nach \\bname\\b suchen.
  3. Die Definitions-Zeile (und weitere 'def name'-Zeilen) abziehen.
  4. Keine weitere Referenz  →  Dead-Code-Kandidat.
  5. Dunders, Entry-Points & Framework-Overrides sind whitelisted.
"""
import ast
import os
import re
import sys

SKIP_DIRS = {
    ".venv", "venv", "working_dir", "__pycache__", "tmp", "data",
    "backups", "rag_db", "tts_server", ".git", "node_modules",
}

# Wird von außen aufgerufen (Framework/Entry), nicht aus unserem Code.
WHITELIST = {
    "main",
    "get_completions",        # prompt_toolkit
    "keyPressEvent", "closeEvent", "run", "processEvent",   # Qt / Thread
    "timerEvent", "resizeEvent", "mousePressEvent",
}


class Def:
    __slots__ = ("file", "line", "name", "kind", "cls")

    def __init__(self, file, line, name, kind, cls=None):
        self.file, self.line, self.name = file, line, name
        self.kind, self.cls = kind, cls


def collect_defs(path, source):
    defs = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return defs

    def visit(node, cls=None, in_func=False):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, cls=child.name, in_func=False)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "nested" if in_func else ("method" if cls else "function")
                defs.append(Def(path, child.lineno, child.name, kind, cls))
                visit(child, cls, in_func=True)
            else:
                visit(child, cls, in_func)

    visit(tree)
    return defs


def build_index(files):
    index = []
    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            index.append((path, f.read().splitlines()))
    return index


def count_usage(name, index, def_file, def_line):
    pat = re.compile(r"\b" + re.escape(name) + r"\b")
    def_pat = re.compile(r"\bdef\s+" + re.escape(name) + r"\b")
    total = 0
    for path, lines in index:
        for i, line in enumerate(lines, 1):
            if not pat.search(line):
                continue
            if path == def_file and i == def_line:
                continue                      # die Definition selbst
            if def_pat.search(line):
                continue                      # weitere def = Duplikat, kein Usage
            total += 1
    return total


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        files += [os.path.join(dirpath, f) for f in filenames if f.endswith(".py")]

    index = build_index(files)
    all_defs = []
    for path, lines in index:
        all_defs += collect_defs(path, "\n".join(lines))

    dead = []
    for d in all_defs:
        if d.name in WHITELIST:
            continue
        if d.name.startswith("__") and d.name.endswith("__"):
            continue                          # Dunder (__init__ etc.)
        if count_usage(d.name, index, d.file, d.line) == 0:
            dead.append(d)

    if not dead:
        print("✅ Keine Karteileichen gefunden.")
        return 0

    by_file = {}
    for d in dead:
        by_file.setdefault(d.file, []).append(d)

    print(f"⚠️  {len(dead)} ungenutzte Funktion(en)/Methode(n):\n")
    for fname in sorted(by_file):
        print(f"── {os.path.relpath(fname, root)}")
        for d in sorted(by_file[fname], key=lambda x: x.line):
            scope = f"{d.cls}." if d.cls else ""
            print(f"   L{d.line:>4}  {d.kind:>8}  {scope}{d.name}()")
        print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
