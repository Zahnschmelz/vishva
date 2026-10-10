"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
#import os
#import re
import json
#import time
#import shutil
#import subprocess
#from typing import Any, Dict, List, Optional, Tuple
from typing import Any, Dict

try:
    import requests
except ImportError:
    requests = None

def _edit_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
    path = self._resolve_path(args.get("path", ""))
    old_string = args.get("old_string")
    new_string = args.get("new_string")
    skip_validation = args.get("skip_validation", False)
    if not path:
        return {"error": "path is required."}
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return {"error": f"Could not read file: {e}"}
    if old_string is None or new_string is None:
        return {
            "error": "old_string and new_string are required.",
            "file_content": content}

    new_content = None
    match_note = ""

    # 1) Exakter Match
    if old_string in content:
        new_content = content.replace(old_string, new_string, 1)
        match_note = "exact match"
    else:
        # 2) Whitespace-normalisierter Match
        def _normalize_ws(s: str) -> str:
            return "\n".join(line.strip() for line in s.strip().split("\n"))
        norm_old = _normalize_ws(old_string)
        norm_content = _normalize_ws(content)
        if norm_old in norm_content:
            old_lines = old_string.strip().split("\n")
            content_lines = content.split("\n")
            for i in range(len(content_lines) - len(old_lines) + 1):
                chunk = content_lines[i:i + len(old_lines)]
                if _normalize_ws("\n".join(chunk)) == norm_old:
                    new_lines = (content_lines[:i]
                                 + new_string.split("\n")
                                 + content_lines[i + len(old_lines):])
                    new_content = "\n".join(new_lines)
                    match_note = f"whitespace-normalized match (line {i + 1})"
                    break
        # 3) Fuzzy: Anchor gefunden, aber Kontext passt nicht
        if new_content is None:
            anchor = old_string.strip()[:50]
            if anchor and anchor in content:
                idx = content.find(anchor)
                context_start = max(0, idx - 200)
                context_end = min(len(content), idx + 200)
                return {
                    "error": f"'old_string' not found exactly, but anchor '{anchor[:30]}...' found. Context does not match.",
                    "file_content": content,
                    "context": content[context_start:context_end],
                    "suggestion": "Use read_file to see current content, then match exactly."}
            return {
                "error": f"'old_string' not found in '{path}' (exact, whitespace-normalized, or fuzzy).",
                "file_content": content,
                "suggestion": "Use read_file to see current content."}

    # No-Op: nichts hat sich geändert → kein Backup, keine Validation
    if new_content == content:
        return {"success": True,
                "message": "No change applied (old_string equals new_string)."}

    # --- Syntax-Validation des RESULTIERENDEN Inhalts ---
    lang = self._detect_language(path, new_content)
    lint_result = None
    if not skip_validation and lang in ("python", "json", "bash"):
        lint_result = self._lint_content(path, new_content, lang)
        if not lint_result["valid"]:
            # Trotzdem schreiben (Backup existiert) — Agent kann den Fehler
            # via weiterem edit_file korrigieren, ohne das File neu zu schreiben.
            return {
                "error": f"Syntax validation failed for {lang} — edit NOT applied.",
                "lint": lint_result,
                "suggestion": "Fix new_string so the resulting file is valid, or set skip_validation=true."}

    # --- Normalfall: valide → schreiben ---
    backup_path = self._backup_file(path)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        return {"error": f"Write failed: {e}"}
    resp = {"success": True,
            "message": f"File '{path}' edited ({match_note}).",
            "lang": lang}
    if backup_path:
        resp["backup"] = backup_path
    if lint_result:
        resp["lint"] = lint_result
    return resp
