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

def _vol_ctl(self, args: Dict[str, Any]) -> Dict[str, Any]:
    action = args.get("action", "")
    percent = args.get("percent", 50)
    if action not in ("vol_up", "vol_down", "vol_percent"):
        return {"error": f"Invalid action: {action}. Use vol_up, vol_down or vol_percent."}
    def _get_current_vol() -> str:
        try:
            r = subprocess.run(
                ["pactl", "list", "sinks"],
                capture_output=True, text=True, timeout=5)
            for line in r.stdout.split("\n"):
                if "Volume:" in line and "%" in line:
                    return line.strip()
        except Exception:
            pass
        return "(unknown)"
    try:
        if action == "vol_up":
            subprocess.run(
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+10%"],
                capture_output=True, text=True, timeout=5, check=False)
            return {"success": True, "message": "Volume +10%", "current": _get_current_vol()}
        elif action == "vol_down":
            subprocess.run(
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-10%"],
                capture_output=True, text=True, timeout=5, check=False)
            return {"success": True, "message": "Volume -10%", "current": _get_current_vol()}
        elif action == "vol_percent":
            if not isinstance(percent, int) or percent < 0 or percent > 100:
                return {"error": "percent must be an integer between 0 and 100."}
            subprocess.run(
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"],
                capture_output=True, text=True, timeout=5, check=False)
            return {"success": True, "message": f"Volume set to {percent}%.", "current": _get_current_vol()}
    except FileNotFoundError:
        try:
            if action == "vol_up":
                subprocess.run(["amixer", "set", "Master", "5%+"], capture_output=True, text=True, timeout=5, check=False)
                return {"success": True, "message": "Volume +5% (amixer)", "current": "(amixer)"}
            elif action == "vol_down":
                subprocess.run(["amixer", "set", "Master", "5%-"], capture_output=True, text=True, timeout=5, check=False)
                return {"success": True, "message": "Volume -5% (amixer)", "current": "(amixer)"}
            elif action == "vol_percent":
                subprocess.run(["amixer", "set", "Master", f"{percent}%"], capture_output=True, text=True, timeout=5, check=False)
                return {"success": True, "message": f"Volume set to {percent}% (amixer).", "current": "(amixer)"}
        except FileNotFoundError:
            return {"error": "Neither pactl nor amixer found. Install pulseaudio-utils or alsa-utils."}
    except Exception as e:
        return {"error": str(e)}
