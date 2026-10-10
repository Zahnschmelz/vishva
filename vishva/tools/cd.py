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

def _cd(self, args: Dict[str, Any]) -> Dict[str, Any]:
    target = os.path.expanduser((args.get("path") or "").strip())
    if not target:
        return {"success": True, "cwd": self.agent_cwd, "message": f"cwd: {self.agent_cwd}"}
    new_dir = self._resolve_path(target)
    if not os.path.isdir(new_dir):
        return {"error": f"no directory: {new_dir}"}
    self.agent_cwd = new_dir
    return {"success": True, "cwd": self.agent_cwd, "message": f"workdir: {new_dir}"}
