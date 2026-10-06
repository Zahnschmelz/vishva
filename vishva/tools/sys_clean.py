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

def _detect_package_manager() -> Optional[str]:
    managers = [
        ("pacman", "pacman -Qdtq"),
        ("apt", "apt list --installed"),
        ("dnf", "dnf list installed"),
        ("zypper", "zypper packages --installed-only"),]
    for mgr, test_cmd in managers:
        if shutil.which(mgr):
            try:
                subprocess.run(test_cmd.split(), capture_output=True, timeout=5)
                return mgr
            except Exception:
                continue
    return None

def _sys_clean(self, args: Dict[str, Any]) -> Dict[str, Any]:
    results = {
        "orphans_removed": [],
        "orphans_error": None,
        "cache_cleaned": False,
        "cache_error": None,
        "package_manager": None}
    pkg_mgr = _detect_package_manager()
    if not pkg_mgr:
        return {
            "success": False,
            "error": "No supported package manager found (pacman/apt/dnf/zypper)"}
    results["package_manager"] = pkg_mgr
    try:
        if pkg_mgr == "pacman":
            orphans = subprocess.run(
                ["pacman", "-Qdtq"],
                capture_output=True, text=True, check=False, timeout=30)
            orphan_list = [p for p in orphans.stdout.strip().split("\n") if p.strip()]
            if orphan_list:
                remove = subprocess.run(
                    ["sudo", "pacman", "-Rns", "--noconfirm"] + orphan_list,
                    capture_output=True, text=True, check=False, timeout=120)
                results["orphans_removed"] = orphan_list
                if remove.returncode != 0:
                    results["orphans_error"] = {
                        "returncode": remove.returncode,
                        "stderr": remove.stderr.strip()[:200],
                        "stdout": remove.stdout.strip()[:200]}
            else:
                results["orphans_removed"] = []
        elif pkg_mgr == "apt":
            remove = subprocess.run(
                ["sudo", "apt", "autoremove", "--purge", "-y"],
                capture_output=True, text=True, check=False, timeout=120)
            if remove.returncode == 0:
                removed = re.findall(r"Removing (\S+)", remove.stdout)
                results["orphans_removed"] = removed
            else:
                results["orphans_error"] = {
                    "returncode": remove.returncode,
                    "stderr": remove.stderr.strip()[:200]}
        elif pkg_mgr == "dnf":
            remove = subprocess.run(
                ["sudo", "dnf", "autoremove", "-y"],
                capture_output=True, text=True, check=False, timeout=120)
            if remove.returncode == 0:
                removed = re.findall(r"Removing\s+(\S+)", remove.stdout)
                results["orphans_removed"] = removed
            else:
                results["orphans_error"] = {
                    "returncode": remove.returncode,
                    "stderr": remove.stderr.strip()[:200]}
        elif pkg_mgr == "zypper":
            remove = subprocess.run(
                ["sudo", "zypper", "rm", "--clean-deps", "-y"],
                capture_output=True, text=True, check=False, timeout=120)
            if remove.returncode == 0:
                results["orphans_removed"] = ["(zypper clean-deps executed)"]
            else:
                results["orphans_error"] = {
                    "returncode": remove.returncode,
                    "stderr": remove.stderr.strip()[:200]}
    except subprocess.TimeoutExpired:
        results["orphans_error"] = "Timeout searching/removing orphan packages."
    except FileNotFoundError:
        results["orphans_error"] = f"{pkg_mgr} not found in PATH."
    except Exception as e:
        results["orphans_error"] = f"{type(e).__name__}: {str(e)}"
    try:
        if pkg_mgr == "pacman":
            cache = subprocess.run(
                ["sudo", "pacman", "-Sc", "--noconfirm"],
                capture_output=True, text=True, check=False, timeout=60)
            results["cache_cleaned"] = cache.returncode == 0
            if cache.returncode != 0:
                results["cache_error"] = {
                    "returncode": cache.returncode,
                    "stderr": cache.stderr.strip()[:200]}
        elif pkg_mgr == "apt":
            cache = subprocess.run(
                ["sudo", "apt", "clean"],
                capture_output=True, text=True, check=False, timeout=60)
            results["cache_cleaned"] = cache.returncode == 0
            if cache.returncode != 0:
                results["cache_error"] = {
                    "returncode": cache.returncode,
                    "stderr": cache.stderr.strip()[:200]}
        elif pkg_mgr == "dnf":
            cache = subprocess.run(
                ["sudo", "dnf", "clean", "all"],
                capture_output=True, text=True, check=False, timeout=60)
            results["cache_cleaned"] = cache.returncode == 0
            if cache.returncode != 0:
                results["cache_error"] = {
                    "returncode": cache.returncode,
                    "stderr": cache.stderr.strip()[:200]}
        elif pkg_mgr == "zypper":
            cache = subprocess.run(
                ["sudo", "zypper", "clean", "--all"],
                capture_output=True, text=True, check=False, timeout=60)
            results["cache_cleaned"] = cache.returncode == 0
            if cache.returncode != 0:
                results["cache_error"] = {
                    "returncode": cache.returncode,
                    "stderr": cache.stderr.strip()[:200]}
    except subprocess.TimeoutExpired:
        results["cache_error"] = "Timeout during cache cleanup."
    except FileNotFoundError:
        results["cache_error"] = f"{pkg_mgr} not found in PATH."
    except Exception as e:
        results["cache_error"] = f"{type(e).__name__}: {str(e)}"
    success = (
        results["orphans_error"] is None and
        results["cache_error"] is None)

    message_parts = []
    if results["orphans_removed"]:
        message_parts.append(f"{len(results['orphans_removed'])} orphan packages removed ({pkg_mgr}).")
    else:
        message_parts.append(f"No orphan packages found ({pkg_mgr}).")

    if results["cache_cleaned"]:
        message_parts.append("Cache cleaned.")
    else:
        message_parts.append("Cache cleanup skipped or failed.")

    return {
        "success": success,
        "results": results,
        "message": " ".join(message_parts)}
