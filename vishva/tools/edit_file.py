"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
import re
import json
import time
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple
try:
    import requests
except ImportError:
    requests = None

def _edit_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
    path = self._resolve_path(args.get("path", ""))
    old_string = args.get("old_string")
    new_string = args.get("new_string")
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
    if old_string in content:
        new_content = content.replace(old_string, new_string, 1)
        backup_path = self._backup_file(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        resp = {"success": True, "message": f"File '{path}' edited."}
        if backup_path:
            resp["backup"] = backup_path
        return resp
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
                new_lines = content_lines[:i] + new_string.split("\n") + content_lines[i + len(old_lines):]
                new_content = "\n".join(new_lines)
                backup_path = self._backup_file(path)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                resp = {
                    "success": True,
                    "message": f"File '{path}' edited (whitespace-normalized match).",
                    "matched_at_line": i + 1}
                if backup_path:
                    resp["backup"] = backup_path
                return resp
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
