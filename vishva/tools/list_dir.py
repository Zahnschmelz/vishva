"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
#import re
#import json
#import time
#import shutil
#import subprocess
#from typing import Any, Dict, List, Optional, Tuple
from typing import Any, Dict

try:
    import requests
except ImportError:
    requests = None

def _list_dir(self, args: Dict[str, Any]) -> Dict[str, Any]:
    path = self._resolve_path(args.get("path", "."))
    max_entries = min(args.get("max_entries", 50), 200)
    show_hidden = args.get("show_hidden", False)
    maxdepth = max(1, min(args.get("maxdepth", 1), 3))
    try:
        details = []
        total = 0
        shown = 0
        truncated = False
        for root, dirs, files in os.walk(path):
            rel_root = os.path.relpath(root, path)
            current_depth = 1 if rel_root == "." else rel_root.count(os.sep) + 2
            if current_depth > maxdepth:
                dirs[:] = []
                continue
            if not show_hidden:
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                files = [f for f in files if not f.startswith(".")]
            dirs.sort()
            files.sort()
            for d in dirs:
                full = os.path.join(root, d)
                try:
                    stat = os.stat(full)
                    size = stat.st_size
                    indent = "  " * (current_depth - 1)
                    details.append(f"{indent}[DIR] {d:<40} {size:>10} B")
                    total += 1
                    shown += 1
                    if shown >= max_entries:
                        truncated = True
                        break
                except Exception:
                    pass
            if truncated:
                break
            for f in files:
                full = os.path.join(root, f)
                try:
                    stat = os.stat(full)
                    size = stat.st_size
                    indent = "  " * (current_depth - 1)
                    details.append(f"{indent}[FIL] {f:<40} {size:>10} B")
                    total += 1
                    shown += 1
                    if shown >= max_entries:
                        truncated = True
                        break
                except Exception:
                    pass
            if truncated:
                break
            if current_depth >= maxdepth:
                dirs[:] = []
        output = "\n".join(details)
        if truncated:
            output += f"\n\n... [weitere Einträge ausgeblendet] ..."
        return {
            "success": True,
            "path": path,
            "total": total,
            "shown": shown,
            "listing": output}
    except Exception as e:
        return {"error": str(e)}
