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

def _send_file(self, args):
    args = args or {}
    file_path = self._resolve_path(args.get("file_path")) or self._resolve_path(args.get("path")) or self._resolve_path(args.get("file"))
    caption = args.get("caption", "")
    if not file_path:
        return {"error": "file_path is required."}
    if not os.path.isfile(file_path):
        return {"error": f"File not found: {file_path}"}
    try:
        from vishva.tools.tg_send_file import send_file_to_telegram
    except Exception as e:
        return {"error": f"tg_send_file import failed: {type(e).__name__}: {e}"}
    result = send_file_to_telegram(file_path)
    return result

