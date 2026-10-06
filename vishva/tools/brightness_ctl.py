"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
import re
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

def _detect_brightness_backend() -> str:
    if shutil.which("qdbus6"):
        try:
            r = subprocess.run(
                ["qdbus6", "org.kde.Solid.PowerManagement",
                 "/org/kde/Solid/PowerManagement/Actions/BrightnessControl",
                 "brightness"],
                capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return "kde"
        except Exception:
            pass
    if shutil.which("gdbus"):
        try:
            r = subprocess.run(
                ["gdbus", "call", "--session", "--dest", "org.gnome.SettingsDaemon.Power",
                 "--object-path", "/org/gnome/SettingsDaemon/Power",
                 "--method", "org.freedesktop.DBus.Properties.Get",
                 "org.gnome.SettingsDaemon.Power.Screen", "Brightness"],
                capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return "gnome"
        except Exception:
            pass
    if os.path.exists("/sys/class/backlight"):
        backlights = os.listdir("/sys/class/backlight")
        if backlights:
            return "sysfs"
    return "none"

def _get_brightness_kde() -> int:
    try:
        result = subprocess.run(
            ["qdbus6", "org.kde.Solid.PowerManagement",
             "/org/kde/Solid/PowerManagement/Actions/BrightnessControl",
             "brightness"],
            capture_output=True, text=True, timeout=5)
        return int(result.stdout.strip())
    except Exception:
        return -1

def _get_brightness_gnome() -> int:
    try:
        result = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.gnome.SettingsDaemon.Power",
             "--object-path", "/org/gnome/SettingsDaemon/Power",
             "--method", "org.freedesktop.DBus.Properties.Get",
             "org.gnome.SettingsDaemon.Power.Screen", "Brightness"],
            capture_output=True, text=True, timeout=5)
        match = re.search(r"int32\s+(\d+)", result.stdout)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    return -1

def _get_brightness_sysfs() -> tuple[int, int]:
    try:
        backlights = os.listdir("/sys/class/backlight")
        if not backlights:
            return -1, -1
        backlight = backlights[0]
        with open(f"/sys/class/backlight/{backlight}/brightness", "r") as f:
            current = int(f.read().strip())
        with open(f"/sys/class/backlight/{backlight}/max_brightness", "r") as f:
            max_val = int(f.read().strip())
        return current, max_val
    except Exception:
        return -1, -1

def _brightness_ctl(self, args: Dict[str, Any]) -> Dict[str, Any]:
    action = args.get("action", "")
    value = args.get("value", 50)
    if action not in ("up", "down", "percent"):
        return {"error": "Invalid action. Use 'up', 'down', or 'percent'."}
    backend = _detect_brightness_backend()
    try:
        if backend == "kde":
            max_val = 10000
            current = _get_brightness_kde()
            if current < 0:
                return {"error": "Failed to read brightness (KDE)."}
            if action == "up":
                new_val = min(max_val, current + 1000)
            elif action == "down":
                new_val = max(0, current - 1000)
            else:
                if not isinstance(value, (int, float)) or value < 0 or value > 100:
                    return {"error": "value must be an integer between 0 and 100."}
                new_val = int((value / 100) * max_val)
            subprocess.run(
                ["qdbus6", "org.kde.Solid.PowerManagement",
                 "/org/kde/Solid/PowerManagement/Actions/BrightnessControl",
                 "setBrightness", str(new_val)],
                capture_output=True, text=True, timeout=5, check=False)
            return {
                "success": True,
                "action": action,
                "previous": current,
                "previous_percent": round((current / max_val) * 100),
                "new": new_val,
                "new_percent": round((new_val / max_val) * 100),
                "backend": "kde"}
        elif backend == "gnome":
            current = _get_brightness_gnome()
            if current < 0:
                return {"error": "Failed to read brightness (GNOME)."}
            if action == "up":
                new_val = min(100, current + 10)
            elif action == "down":
                new_val = max(0, current - 10)
            else:
                if not isinstance(value, (int, float)) or value < 0 or value > 100:
                    return {"error": "value must be an integer between 0 and 100."}
                new_val = int(value)
            subprocess.run(
                ["gdbus", "call", "--session", "--dest", "org.gnome.SettingsDaemon.Power",
                 "--object-path", "/org/gnome/SettingsDaemon/Power",
                 "--method", "org.freedesktop.DBus.Properties.Set",
                 "org.gnome.SettingsDaemon.Power.Screen", "Brightness",
                 f"<int32 {new_val}>"],
                capture_output=True, text=True, timeout=5, check=False)
            return {
                "success": True,
                "action": action,
                "previous": current,
                "previous_percent": current,
                "new": new_val,
                "new_percent": new_val,
                "backend": "gnome"}
        elif backend == "sysfs":
            current, max_val = _get_brightness_sysfs()
            if current < 0 or max_val < 0:
                return {"error": "Failed to read brightness (sysfs)."}
            if action == "up":
                new_val = min(max_val, current + (max_val // 10))
            elif action == "down":
                new_val = max(0, current - (max_val // 10))
            else:
                if not isinstance(value, (int, float)) or value < 0 or value > 100:
                    return {"error": "value must be an integer between 0 and 100."}
                new_val = int((value / 100) * max_val)
            backlights = os.listdir("/sys/class/backlight")
            backlight = backlights[0]
            with open(f"/sys/class/backlight/{backlight}/brightness", "w") as f:
                f.write(str(new_val))
            return {
                "success": True,
                "action": action,
                "previous": current,
                "previous_percent": round((current / max_val) * 100),
                "new": new_val,
                "new_percent": round((new_val / max_val) * 100),
                "backend": "sysfs"}
        else:
            return {"error": "No brightness control backend found (KDE/GNOME/sysfs)."}
    except Exception as e:
        return {"error": f"Failed to set brightness: {type(e).__name__}: {e}"}
