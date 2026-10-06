"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
import re
import json
import time
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

def _read_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
    path = self._resolve_path(args.get("path", ""))
    if not path:
        return {"error": "path is required."}
    try:
        MAX_READ_SIZE = self.config.get("max_read_file_size", 100_000)
        size = os.path.getsize(path)
        if size > MAX_READ_SIZE:
            with open(path, "r", encoding="utf-8") as f:
                head = "".join(f.readline() for _ in range(100))
            with open(path, "r", encoding="utf-8") as f:
                f.seek(max(0, size - 5000))
                tail = f.read()
            return {
                "success": True,
                "content": head + f"\n\n... [Datei zu groß: {size} Bytes, nur Head/Tail] ...\n\n" + tail,
                "truncated": True,
                "size": size}
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"success": True, "content": content}
    except Exception as e:
        return {"error": str(e)}
