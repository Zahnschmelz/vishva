"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
import re
import sys
import json
import time
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple
from ..paths import p, cfg_path, BASE_DIR
try:
    import requests
except ImportError:
    requests = None

def _news_digest(self, args: Dict[str, Any]) -> Dict[str, Any]:
    topics = args.get("topics", [])
    if not topics or not isinstance(topics, list):
        return {"error": "topics must be a non-empty list of strings."}
    script_path = p("vishva", "tools", "news.py")
    if not os.path.exists(script_path):
        return {"error": f"News digest script not found: {script_path}"}
    clean_topics = [t for t in topics if isinstance(t, str) and t.strip()]
    if not clean_topics:
        return {"error": "No valid topics provided."}
    try:
        result = subprocess.run(
            [sys.executable, script_path] + clean_topics,
            capture_output=True,
            text=True,
            timeout=600)
        output = result.stdout.strip()
        if not output:
            output = result.stderr.strip() or "(Keine Ausgabe)"
        try:
            data = json.loads(output)
            if "error" in data and not data.get("success"):
                return data
            return {
                "success": True,
                "summary": data.get("summary", ""),
                "topics_searched": data.get("topics_searched", []),
                "total_articles_scanned": data.get("total_articles_scanned", 0),
                "warning": data.get("warning", "")}
        except json.JSONDecodeError:
            return {"success": True, "raw_output": output}
    except subprocess.TimeoutExpired:
        return {"error": "Timeout after 600 seconds."}
    except Exception as e:
        return {"error": str(e)}
