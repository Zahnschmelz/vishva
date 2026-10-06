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


def _write_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
    path = self._resolve_path(args.get("path", ""))
    content = args.get("content", "")
    skip_validation = args.get("skip_validation", False)
    ok, err = self._validate_path(path, allow_write=True)
    if not ok:
        return {"error": err}
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    except Exception as e:
        return {"error": f"Cannot create directory: {e}"}
    lang = self._detect_language(path, content)
    lint_result = None
    if not skip_validation and lang in ("python", "json", "bash"):
        lint_result = self._lint_content(path, content, lang)
        if not lint_result["valid"]:
            return {
                "error": f"Syntax validation failed for {lang}",
                "lint": lint_result,
                "suggestion": "Fix syntax errors or set skip_validation=true to force write."}
    backup_path = self._backup_file(path)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        msg = (f"File '{path}' overwritten (backup: {backup_path})"
               if backup_path else f"File '{path}' created.")
        resp = {"success": True, "message": msg, "lang": lang}
        if backup_path:
            resp["backup"] = backup_path
        if lint_result:
            resp["lint"] = lint_result
        return resp
    except Exception as e:
        return {"error": str(e)}

