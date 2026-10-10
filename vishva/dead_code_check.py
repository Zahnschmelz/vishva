#!/usr/bin/env python3
import ast
import os
import re
import sys

SKIP_DIRS = {
    ".venv", "venv", "working_dir", "__pycache__", "tmp", "data",
    "backups", "rag_db", "tts_server", ".git", "node_modules",}

WHITELIST = {
    "main",
    "get_completions",
    "keyPressEvent", "closeEvent", "run", "processEvent",
    "timerEvent", "resizeEvent", "mousePressEvent",}

# Imports, die nie als "tot" gemeldet werden sollen (Side-Effects, bewusste Re-Exports)
IMPORT_WHITELIST = {
    # z. B. "atexit",
}


class Def:
    __slots__ = ("file", "line", "name", "kind", "cls")
    def __init__(self, file, line, name, kind, cls=None):
        self.file, self.line, self.name = file, line, name
        self.kind, self.cls = kind, cls


class Imp:
    __slots__ = ("file", "line", "name", "module", "kind")
    def __init__(self, file, line, name, module, kind):
        self.file, self.line, self.name = file, line, name
        self.module, self.kind = module, kind


def parse_source(source):
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def collect_defs(path, tree):
    defs = []
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


def collect_imports(path, tree):
    imps = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                imps.append(Imp(path, node.lineno, local, alias.name, "import"))
        elif isinstance(node, ast.ImportFrom):
            module = ("." * node.level) + (node.module or "")
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                imps.append(Imp(path, node.lineno, local, module, "from"))
    return imps


def import_line_ranges(tree):
    """Alle Zeilen, die zu Import-Statements gehören (auch mehrzeilige)."""
    ranges = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            ranges.update(range(node.lineno, end + 1))
    return ranges


def build_index(files):
    index = []      # (path, lines)
    trees = {}      # path -> ast | None
    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        index.append((path, source.splitlines()))
        trees[path] = parse_source(source)
    return index, trees


def count_usage(name, index, def_file, def_line):
    pat = re.compile(r"\b" + re.escape(name) + r"\b")
    def_pat = re.compile(r"\bdef\s+" + re.escape(name) + r"\b")
    total = 0
    for path, lines in index:
        for i, line in enumerate(lines, 1):
            if not pat.search(line):
                continue
            if path == def_file and i == def_line:
                continue
            if def_pat.search(line):
                continue
            total += 1
    return total


def count_usage_in_file(name, lines, skip_lines):
    """Zählt Nutzungen eines Namens NUR in einer Datei (Imports sind dateilokal)."""
    pat = re.compile(r"\b" + re.escape(name) + r"\b")
    total = 0
    for i, line in enumerate(lines, 1):
        if i in skip_lines:
            continue
        if pat.search(line):
            total += 1
    return total


def build_reexport_map(files, trees):
    """Menge (pfad, name): 'name' wird in 'pfad' importiert und von einer
    ANDEREN Datei via 'from <pfad-modul> import name' weiterverwendet.
    Solche Imports sind Re-Exports und NICHT tot."""
    base_to_path = {}
    for path in files:
        base = os.path.splitext(os.path.basename(path))[0]
        base_to_path[base] = None if base in base_to_path else path
    reexported = set()
    for path, tree in trees.items():
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            base = module.split(".")[-1] if module else ""
            target = base_to_path.get(base)
            if not target or target == path:
                continue
            for alias in node.names:
                if alias.name != "*":
                    reexported.add((target, alias.name))
    return reexported


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        files += [os.path.join(dirpath, f) for f in filenames if f.endswith(".py")]

    index, trees = build_index(files)
    lines_by_path = {path: lines for path, lines in index}

    all_defs, all_imps = [], []
    skip_lines_by_path = {}
    for path, tree in trees.items():
        if tree is None:
            continue
        all_defs += collect_defs(path, tree)
        all_imps += collect_imports(path, tree)
        skip_lines_by_path[path] = import_line_ranges(tree)

    # --- tote Funktionen / Methoden ---
    dead = []
    for d in all_defs:
        if d.name in WHITELIST:
            continue
        if d.name.startswith("__") and d.name.endswith("__"):
            continue
        if count_usage(d.name, index, d.file, d.line) == 0:
            dead.append(d)

    # --- tote Imports ---
    reexported = build_reexport_map(files, trees)
    dead_imps = []
    for imp in all_imps:
        if imp.name in IMPORT_WHITELIST:
            continue
        if (imp.file, imp.name) in reexported:
            continue                      # wird von anderem Modul weiterimportiert
        used = count_usage_in_file(imp.name, lines_by_path[imp.file],
                                   skip_lines_by_path[imp.file])
        if used == 0:
            dead_imps.append(imp)

    if not dead and not dead_imps:
        print("✅ Keine Karteileichen gefunden (weder Funktionen noch Imports).")
        return 0

    if dead:
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

    if dead_imps:
        by_file = {}
        for imp in dead_imps:
            by_file.setdefault(imp.file, []).append(imp)
        print(f"⚠️  {len(dead_imps)} ungenutzte(r) Import(e):\n")
        for fname in sorted(by_file):
            print(f"── {os.path.relpath(fname, root)}")
            for imp in sorted(by_file[fname], key=lambda x: x.line):
                if imp.kind == "from":
                    print(f"   L{imp.line:>4}   {imp.name}  (from {imp.module})")
                else:
                    print(f"   L{imp.line:>4}   import {imp.name}")
            print()

    return 1


if __name__ == "__main__":
    sys.exit(main())
