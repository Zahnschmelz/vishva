"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
#import re
#import json
#import time
import shutil
import subprocess
#from typing import Any, Dict, List, Optional, Tuple
from typing import Any, Dict
#from ..paths import p, cfg_path, BASE_DIR

try:
    import requests
except ImportError:
    requests = None

def _detect_desktop_environment() -> str:
    de = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if "kde" in de:
        return "kde"
    elif "gnome" in de:
        return "gnome"
    elif "xfce" in de:
        return "xfce"
    elif "mate" in de:
        return "mate"
    return "unknown"

def _turnoff_screen(self, args: Dict[str, Any]) -> Dict[str, Any]:
    de = _detect_desktop_environment()
    try:
        if de == "kde":
            result = subprocess.run(
                "/bin/sleep 1 && /bin/dbus-send --session --print-reply "
                "--dest=org.kde.kglobalaccel /component/org_kde_powerdevil "
                "org.kde.kglobalaccel.Component.invokeShortcut string:'Turn Off Screen'",
                shell=True,
                capture_output=True,
                text=True,
                timeout=30)
            if result.returncode == 0:
                return {"success": True, "message": "Screen turned off (KDE)", "backend": "kde"}
            else:
                return {
                    "success": False,
                    "message": f"KDE dbus-send failed (exit {result.returncode})",
                    "stderr": result.stderr.strip()[:200] if result.stderr else "",
                    "backend": "kde"}

        elif de == "gnome":
            result = subprocess.run(
                ["xset", "dpms", "force", "off"],
                capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return {"success": True, "message": "Screen turned off (GNOME/xset)", "backend": "gnome"}

        elif de in ("xfce", "mate"):
            result = subprocess.run(
                ["xset", "dpms", "force", "off"],
                capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return {"success": True, "message": f"Screen turned off ({de}/xset)", "backend": de}

        if shutil.which("xset"):
            result = subprocess.run(
                ["xset", "dpms", "force", "off"],
                capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return {"success": True, "message": "Screen turned off (xset DPMS)", "backend": "xset"}

        if shutil.which("swaymsg"):
            result = subprocess.run(
                ["swaymsg", "output", "*", "dpms", "off"],
                capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return {"success": True, "message": "Screen turned off (swaymsg)", "backend": "sway"}
        return {
            "success": False,
            "message": "Failed to turn off screen. No supported method found.",
            "desktop_environment": de}

    except subprocess.TimeoutExpired:
        return {"success": False, "message": "Timeout while turning off screen."}
    except Exception as e:
        return {"success": False, "message": f"Error: {type(e).__name__}: {e}"}
