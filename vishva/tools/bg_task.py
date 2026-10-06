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

def _bg_task(self, args: Dict[str, Any]) -> Dict[str, Any]:
    action = str(args.get("action", "start")).lower()
    if action == "start":
        return self.bg_manager.start(
            command=args.get("command", ""),
            description=args.get("description", ""),
            restart_on_failure=bool(args.get("restart_on_failure", False)))
    if action == "status":
        return self.bg_manager.status(args.get("task_id", ""))
    if action == "list":
        return self.bg_manager.list_tasks()
    if action == "stop":
        return self.bg_manager.stop(args.get("task_id", ""))
    if action == "restart":
        return self.bg_manager.restart(args.get("task_id", ""))
    if action == "clean":
        return self.bg_manager.clean()
    return {"error": f"Unknown bg_task action: {action}"}
