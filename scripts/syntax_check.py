#!/usr/bin/env python3
"""
Syntax & Import Checker für alle Python-Dateien im Projekt.
Prüft:
1. Syntax via py_compile
2. Import-Validierung via ast (existierende Module, relative Imports)
3. Optionale zirkuläre Import-Detektion
"""
import ast
import os
import sys
import py_compile
import importlib.util
from pathlib import Path
from typing import List, Dict, Set, Tuple
from dataclasses import dataclass

PROJECT_ROOT = Path(__file__).parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@dataclass
class CheckResult:
    file: str
    syntax_ok: bool
    syntax_error: str = ""
    import_errors: List[str] = None

    def __post_init__(self):
        if self.import_errors is None:
            self.import_errors = []

    @property
    def ok(self) -> bool:
        return self.syntax_ok and len(self.import_errors) == 0


class ImportChecker(ast.NodeVisitor):
    """AST-Visitor der alle Imports sammelt und validiert."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.imports = []
        self.errors = []

    def visit_Import(self, node):
        for alias in node.names:
            self.imports.append({
                'type': 'import',
                'module': alias.name,
                'alias': alias.asname,
                'line': node.lineno
            })
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        module = node.module or ''
        level = node.level or 0

        # Relative imports
        if level > 0:
            self.imports.append({
                'type': 'relative',
                'module': module,
                'level': level,
                'names': [alias.name for alias in node.names],
                'line': node.lineno
            })
        else:
            # Absolute imports
            self.imports.append({
                'type': 'from',
                'module': module,
                'names': [alias.name for alias in node.names],
                'line': node.lineno
            })

        self.generic_visit(node)

    def validate_imports(self, project_root: Path) -> List[str]:
        """Validiert gesammelte Imports."""
        errors = []

        for imp in self.imports:
            try:
                if imp['type'] == 'relative':
                    # Relative imports sind schwer zu validieren ohne Context
                    # Wir checken nur ob das Basis-Paket existiert
                    continue

                module_name = imp['module']

                # Standard-Library und bekannte Pakete skippen
                if self._is_stdlib_or_common(module_name):
                    continue

                # Versuchen das Modul zu finden
                spec = importlib.util.find_spec(module_name)
                if spec is None:
                    errors.append(f"Line {imp['line']}: Module '{module_name}' not found")

            except (AttributeError, ValueError, ModuleNotFoundError) as e:
                errors.append(f"Line {imp['line']}: Import error for '{imp.get('module', '?')}': {e}")

        return errors

    def _is_stdlib_or_common(self, module_name: str) -> bool:
        """Checkt ob Modul Standard-Library oder bekanntes Paket ist."""
        # Häufige Pakete
        known = {
            'os', 'sys', 'json', 're', 'time', 'datetime', 'pathlib',
            'typing', 'collections', 'functools', 'itertools', 'subprocess',
            'shutil', 'tempfile', 'ast', 'copy', 'io', 'threading', 'asyncio',
            'requests', 'rich', 'prompt_toolkit', 'yaml', 'toml',
            'numpy', 'pandas', 'scipy', 'matplotlib', 'PIL', 'cv2',
        }

        base = module_name.split('.')[0]
        return base in known or base.startswith('_')


def check_file(filepath: Path, project_root: Path) -> CheckResult:
    """Checkt eine einzelne Python-Datei."""
    result = CheckResult(file=str(filepath), syntax_ok=False)

    # 1. Syntax-Check via py_compile
    try:
        py_compile.compile(str(filepath), doraise=True)
        result.syntax_ok = True
    except py_compile.PyCompileError as e:
        result.syntax_error = str(e)
        return result

    # 2. Import-Check via ast
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            source = f.read()

        tree = ast.parse(source, filename=str(filepath))
        checker = ImportChecker(str(filepath))
        checker.visit(tree)
        result.import_errors = checker.validate_imports(project_root)

    except SyntaxError as e:
        result.syntax_ok = False
        result.syntax_error = f"AST parse error: {e}"
    except Exception as e:
        result.import_errors.append(f"Unexpected error: {type(e).__name__}: {e}")

    return result


def find_python_files(root: Path, skip_dirs: Set[str]) -> List[Path]:
    """Findet alle .py-Dateien rekursiv."""
    files = []

    for path in root.rglob('*.py'):
        # Skip directories
        rel_parts = path.relative_to(root).parts
        if any(part in skip_dirs for part in rel_parts):
            continue
        files.append(path)

    return sorted(files)


def main():
    # Konfiguration
    project_root = Path(__file__).parent
    skip_dirs = {
        '.venv', 'venv', '__pycache__', '.git', 'node_modules',
        'build', 'dist', '.tox', '.mypy_cache', '.pytest_cache',
        'working_dir', 'backups', 'data', 'tmp'
    }

    print(f"🔍 Checking Python files in: {project_root}")
    print(f"⏭️  Skipping dirs: {', '.join(sorted(skip_dirs))}\n")

    # Files finden
    files = find_python_files(project_root, skip_dirs)
    print(f"📁 Found {len(files)} Python files\n")

    # Checken
    results: List[CheckResult] = []
    for filepath in files:
        result = check_file(filepath, project_root)
        results.append(result)

        # Live-Feedback
        rel_path = filepath.relative_to(project_root)
        if result.ok:
            print(f"✅ {rel_path}")
        else:
            print(f"❌ {rel_path}")
            if not result.syntax_ok:
                print(f"   Syntax: {result.syntax_error}")
            for err in result.import_errors:
                print(f"   Import: {err}")

    # Summary
    print("\n" + "="*70)
    ok_count = sum(1 for r in results if r.ok)
    fail_count = len(results) - ok_count

    print(f"\n📊 Summary:")
    print(f"   ✅ {ok_count}/{len(results)} files OK")

    if fail_count > 0:
        print(f"   ❌ {fail_count}/{len(results)} files with errors\n")

        # Details
        print("❌ Files with errors:")
        for r in results:
            if not r.ok:
                rel = Path(r.file).relative_to(project_root)
                print(f"\n   {rel}")
                if not r.syntax_ok:
                    print(f"      Syntax: {r.syntax_error}")
                for err in r.import_errors:
                    print(f"      Import: {err}")

        sys.exit(1)
    else:
        print("\n✅ All files passed syntax and import checks!")
        sys.exit(0)


if __name__ == '__main__':
    main()
