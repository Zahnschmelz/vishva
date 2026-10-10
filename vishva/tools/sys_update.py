"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
#import os
#import re
#import json
#import time
import shutil
import subprocess
#from typing import Any, Dict, List, Optional, Tuple
from typing import Any, Dict

try:
    import requests
except ImportError:
    requests = None

def _detect_audio_backend() -> str:
    if shutil.which("pactl"):
        try:
            r = subprocess.run(
                ["pactl", "info"],
                capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return "pactl"
        except Exception:
            pass
    if shutil.which("wpctl"):
        try:
            r = subprocess.run(
                ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
                capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return "wpctl"
        except Exception:
            pass
    if shutil.which("amixer"):
        return "amixer"

    return "none"

def _get_current_vol_pactl() -> str:
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

def _get_current_vol_wpctl() -> str:
    try:
        r = subprocess.run(
            ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
            capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception:
        return "(unknown)"

def _get_current_vol_amixer() -> str:
    try:
        r = subprocess.run(
            ["amixer", "get", "Master"],
            capture_output=True, text=True, timeout=5)
        for line in r.stdout.split("\n"):
            if "%" in line:
                return line.strip()
    except Exception:
        pass
    return "(unknown)"

def _vol_ctl(self, args: Dict[str, Any]) -> Dict[str, Any]:
    action = args.get("action", "")
    percent = args.get("percent", 50)
    if action not in ("vol_up", "vol_down", "vol_percent"):
        return {"error": f"Invalid action: {action}. Use vol_up, vol_down or vol_percent."}
    backend = _detect_audio_backend()
    try:
        if backend == "pactl":
            if action == "vol_up":
                subprocess.run(
                    ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+10%"],
                    capture_output=True, text=True, timeout=5, check=False)
                return {"success": True, "message": "Volume +10%", "current": _get_current_vol_pactl(), "backend": "pactl"}
            elif action == "vol_down":
                subprocess.run(
                    ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-10%"],
                    capture_output=True, text=True, timeout=5, check=False)
                return {"success": True, "message": "Volume -10%", "current": _get_current_vol_pactl(), "backend": "pactl"}
            elif action == "vol_percent":
                if not isinstance(percent, int) or percent < 0 or percent > 100:
                    return {"error": "percent must be an integer between 0 and 100."}
                subprocess.run(
                    ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"],
                    capture_output=True, text=True, timeout=5, check=False)
                return {"success": True, "message": f"Volume set to {percent}%.", "current": _get_current_vol_pactl(), "backend": "pactl"}
        elif backend == "wpctl":
            if action == "vol_up":
                subprocess.run(
                    ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "10%+"],
                    capture_output=True, text=True, timeout=5, check=False)
                return {"success": True, "message": "Volume +10%", "current": _get_current_vol_wpctl(), "backend": "wpctl"}
            elif action == "vol_down":
                subprocess.run(
                    ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "10%-"],
                    capture_output=True, text=True, timeout=5, check=False)
                return {"success": True, "message": "Volume -10%", "current": _get_current_vol_wpctl(), "backend": "wpctl"}
            elif action == "vol_percent":
                if not isinstance(percent, int) or percent < 0 or percent > 100:
                    return {"error": "percent must be an integer between 0 and 100."}
                subprocess.run(
                    ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{percent}%"],
                    capture_output=True, text=True, timeout=5, check=False)
                return {"success": True, "message": f"Volume set to {percent}%.", "current": _get_current_vol_wpctl(), "backend": "wpctl"}
        elif backend == "amixer":
            if action == "vol_up":
                subprocess.run(["amixer", "set", "Master", "5%+"], capture_output=True, text=True, timeout=5, check=False)
                return {"success": True, "message": "Volume +5% (amixer)", "current": _get_current_vol_amixer(), "backend": "amixer"}
            elif action == "vol_down":
                subprocess.run(["amixer", "set", "Master", "5%-"], capture_output=True, text=True, timeout=5, check=False)
                return {"success": True, "message": "Volume -5% (amixer)", "current": _get_current_vol_amixer(), "backend": "amixer"}
            elif action == "vol_percent":
                subprocess.run(["amixer", "set", "Master", f"{percent}%"], capture_output=True, text=True, timeout=5, check=False)
                return {"success": True, "message": f"Volume set to {percent}% (amixer).", "current": _get_current_vol_amixer(), "backend": "amixer"}
        else:
            return {"error": "No audio control backend found. Install pulseaudio-utils, wireplumber, or alsa-utils."}
    except FileNotFoundError:
        return {"error": f"{backend} command not found."}
    except Exception as e:
        return {"error": str(e)}
