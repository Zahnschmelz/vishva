"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
import re
import ast
import json
import time
import shutil
import tempfile
import subprocess
from typing import Any, Dict, List, Optional, Tuple
try:
    import requests
except ImportError:
    requests = None

def _lint_code(self, args: Dict[str, Any]) -> Dict[str, Any]:
    content = args.get("content", "")
    path = args.get("path", "")
    lang_override = args.get("lang", "auto")
    if not content:
        return {"error": "content is required."}
    if lang_override != "auto":
        lang = lang_override
    elif path:
        lang = self._detect_language(path, content)
    else:
        if content.strip().startswith("{") or content.strip().startswith("["):
            lang = "json"
        elif content.strip().startswith("#!/bin/bash") or content.strip().startswith("#!/bin/sh"):
            lang = "bash"
        elif "def " in content or "import " in content or "class " in content:
            lang = "python"
        elif "function " in content or "const " in content or "let " in content:
            lang = "javascript"
        else:
            lang = "unknown"
    if lang == "unknown":
        return {"error": "Could not detect language. Specify 'lang' parameter.", "supported": ["python", "json", "bash", "javascript"]}
    if lang not in ("python", "json", "bash", "javascript"):
        return {"error": f"Language '{lang}' not supported for linting.", "supported": ["python", "json", "bash", "javascript"]}
    result = self._lint_content(path or "<inline>", content, lang)
    if lang == "javascript":
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False) as f:
                f.write(content)
                tmp = f.name
            r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True, timeout=5)
            if r.returncode != 0:
                result["valid"] = False
                result["errors"].append(f"JavaScript syntax error: {r.stderr.strip()}")
            else:
                result["node_valid"] = True
            os.remove(tmp)
        except FileNotFoundError:
            result["warnings"].append("node not available for JS syntax check")
        except Exception as e:
            result["warnings"].append(f"JS check failed: {e}")
    return {
        "success": result["valid"],
        "lang": lang,
        "valid": result["valid"],
        "errors": result["errors"],
        "warnings": result["warnings"],
        "details": {k: v for k, v in result.items() if k not in ("valid", "errors", "warnings", "lang")}}

def _detect_language(self, path: str, content: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    lang_map = {
        ".py": "python",
        ".json": "json",
        ".sh": "bash",
        ".bash": "bash",
        ".js": "javascript",
        ".ts": "typescript",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".md": "markdown",
        ".html": "html",
        ".css": "css",
        ".rs": "rust",
        ".go": "go",
        ".c": "c",
        ".cpp": "cpp",
        ".h": "c",
        ".hpp": "cpp",}
    if ext in lang_map:
        return lang_map[ext]
    if content.strip().startswith("{") or content.strip().startswith("["):
        return "json"
    if content.strip().startswith("#!/bin/bash") or content.strip().startswith("#!/bin/sh"):
        return "bash"
    if "def " in content or "import " in content or "class " in content:
        return "python"
    return "unknown"

def _lint_content(self, path: str, content: str, lang: str) -> Dict[str, Any]:
    result = {"valid": True, "errors": [], "warnings": [], "lang": lang}
    if lang == "python":
        try:
            ast.parse(content)
            result["ast_valid"] = True
        except SyntaxError as e:
            result["valid"] = False
            result["ast_valid"] = False
            result["errors"].append(f"Python SyntaxError line {e.lineno}: {e.msg}")
        except Exception as e:
            result["valid"] = False
            result["errors"].append(f"Python parse error: {e}")
        if "except:" in content and "except Exception" not in content:
            result["warnings"].append("Bare 'except:' found - catches KeyboardInterrupt and SystemExit")
        if "eval(" in content:
            result["warnings"].append("eval() detected - security risk")
        if "exec(" in content:
            result["warnings"].append("exec() detected - security risk")
    elif lang == "json":
        try:
            json.loads(content)
            result["json_valid"] = True
        except json.JSONDecodeError as e:
            result["valid"] = False
            result["json_valid"] = False
            result["errors"].append(f"JSON error: {e}")
    elif lang == "bash":
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("rm -rf /"):
                result["warnings"].append(f"Line {i}: Dangerous 'rm -rf /' detected")
            if "sudo " in stripped and "rm " in stripped:
                result["warnings"].append(f"Line {i}: sudo + rm combination - risky")
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
                f.write(content)
                tmp = f.name
            r = subprocess.run(["bash", "-n", tmp], capture_output=True, text=True, timeout=5)
            if r.returncode != 0:
                result["valid"] = False
                result["errors"].append(f"Bash syntax error: {r.stderr.strip()}")
            os.remove(tmp)
        except FileNotFoundError:
            result["warnings"].append("bash not available for syntax check")
        except Exception as e:
            result["warnings"].append(f"Bash check failed: {e}")
    elif lang == "javascript":
        open_braces = content.count("{")
        close_braces = content.count("}")
        if open_braces != close_braces:
            result["warnings"].append(f"Brace mismatch: {open_braces} open, {close_braces} close")
        open_parens = content.count("(")
        close_parens = content.count(")")
        if open_parens != close_parens:
            result["warnings"].append(f"Parenthesis mismatch: {open_parens} open, {close_parens} close")
        if "== " in content and "=== " not in content:
            result["warnings"].append("Loose equality (==) found - consider strict equality (===)")
        if "var " in content:
            result["warnings"].append("'var' found - consider 'let' or 'const'")
    return result
