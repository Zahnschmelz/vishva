"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
#import re
#import json
#import time
##import shutil
#import subprocess
#from typing import Any, Dict, List, Optional, Tuple
from typing import Any, Dict

try:
    import requests
except ImportError:
    requests = None

def _send_image(self, args: Dict[str, Any]) -> Dict[str, Any]:
    path = self._resolve_path(args.get("path", ""))
    caption = args.get("caption", "")
    if not path:
        return {"error": "path is required."}
    if not os.path.exists(path):
        return {"error": f"Image not found: {path}"}
    return {"success": True, "path": path, "caption": caption, "marker": f"[SEND_IMAGE:{path}]"}
