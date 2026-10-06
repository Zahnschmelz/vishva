import os
import json
import uuid
import time
import shutil
import subprocess
import datetime
from typing import Dict, Any, Optional, List
from .paths import p


class BackgroundTaskManager:
    """Führt Hintergrund-Tasks als transiente systemd-User-Services aus.
    Command/Status/Logs werden in data/bg_tasks/ persistiert, damit
    Restart und Neustart-Erkennung möglich sind."""

    def __init__(self, config: dict):
        self.config = config or {}
        self.base_dir = p("data", "bg_tasks")
        self.scripts_dir = os.path.join(self.base_dir, "scripts")
        self.logs_dir = os.path.join(self.base_dir, "logs")
        os.makedirs(self.scripts_dir, exist_ok=True)
        os.makedirs(self.logs_dir, exist_ok=True)
        self.initial_delay = float(self.config.get("bg_task_initial_delay", 4))
        self.log_tail_lines = int(self.config.get("bg_task_log_tail_lines", 15))

    # ---------- Pfade ----------
    def _meta_path(self, tid):   return os.path.join(self.base_dir, f"{tid}.json")
    def _exit_path(self, tid):   return os.path.join(self.base_dir, f"{tid}.exit")
    def _script_path(self, tid): return os.path.join(self.scripts_dir, f"{tid}.sh")
    def _log_path(self, tid):    return os.path.join(self.logs_dir, f"{tid}.log")
    def _unit_name(self, tid):   return f"vishva-bg-{tid}"

    # ---------- Meta ----------
    def _load_meta(self, tid) -> Dict[str, Any]:
        path = self._meta_path(tid)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_meta(self, tid, meta):
        with open(self._meta_path(tid), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

    def _remove_artifacts(self, tid):
        for path in (self._exit_path(tid), self._log_path(tid), self._script_path(tid)):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

    # ---------- systemd-Helfer ----------
    def _systemd_available(self) -> bool:
        return shutil.which("systemd-run") is not None

    def _systemctl_show(self, unit, props) -> Dict[str, str]:
        try:
            args = ["systemctl", "--user", "show", unit] + [f"-p{pr}" for pr in props]
            r = subprocess.run(args, capture_output=True, text=True, timeout=10)
            out = {}
            for line in r.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k] = v
            return out
        except Exception:
            return {}

    def _log_tail(self, tid) -> str:
        log = self._log_path(tid)
        if not os.path.exists(log):
            return ""
        try:
            with open(log, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return "".join(lines[-self.log_tail_lines:])
        except Exception:
            return ""

    # ---------- Status ----------
    def _query_status(self, tid) -> Dict[str, Any]:
        meta = self._load_meta(tid)
        unit = meta.get("unit", self._unit_name(tid))
        props = self._systemctl_show(unit,
            ["LoadState", "ActiveState", "SubState", "ExecMainStatus", "Result"])
        load = props.get("LoadState", "not-found")
        active = props.get("ActiveState", "")
        sub = props.get("SubState", "")

        exit_code = None
        exit_path = self._exit_path(tid)
        if os.path.exists(exit_path):
            try:
                exit_code = int(open(exit_path, "r").read().strip())
            except Exception:
                exit_code = None

        if load == "not-found":
            if exit_code is not None:
                if exit_code == 0:
                    return {"status": "completed", "exit_code": 0, "detail": "finished OK"}
                return {"status": "failed", "exit_code": exit_code,
                        "detail": self._log_tail(tid) or f"exit code {exit_code}"}
            if meta.get("status") == "running":
                return {"status": "interrupted",
                        "detail": "Unit weg, kein Exit-Code (Neustart oder gekillt?). Restart möglich."}
            return {"status": "unknown", "detail": "unit not found"}

        if active == "active" and sub == "running":
            return {"status": "running", "detail": "in progress"}
        if active == "failed":
            return {"status": "failed", "exit_code": exit_code,
                    "detail": self._log_tail(tid) or "failed"}
        if active in ("inactive", "deactivating", "activating"):
            if exit_code is not None:
                status = "completed" if exit_code == 0 else "failed"
                return {"status": status, "exit_code": exit_code,
                        "detail": "" if exit_code == 0 else self._log_tail(tid)}
            return {"status": active, "detail": sub}
        return {"status": active or "unknown", "detail": sub}

    # ---------- Aktionen ----------
    def start(self, command: str, description: str = "",
              restart_on_failure: bool = False, task_id: Optional[str] = None) -> Dict[str, Any]:
        if not command or not command.strip():
            return {"success": False, "error": "command ist leer"}
        if not self._systemd_available():
            return {"success": False, "error": "systemd-run nicht verfügbar (nur Linux mit systemd)"}

        tid = task_id or uuid.uuid4().hex[:8]
        unit = self._unit_name(tid)
        log_path = self._log_path(tid)
        exit_path = self._exit_path(tid)
        script_path = self._script_path(tid)
        workdir = os.path.abspath(self.config.get("agent_workdir", "working_dir"))
        os.makedirs(workdir, exist_ok=True)

        # Launcher-Skript schreiben (vermeidet Quoting-Probleme)
        script = (
            "#!/usr/bin/env bash\n"
            f"cd \"{workdir}\" || exit 1\n"
            f"( {command} ) > \"{log_path}\" 2>&1\n"
            "__ec=$?\n"
            f"echo \"$__ec\" > \"{exit_path}\"\n"
            "exit $__ec\n")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)
        os.chmod(script_path, 0o755)

        cmd = ["systemd-run", "--user", f"--unit={unit}",
               f"--description={description or f'Vishva BG task {tid}'}"]
        if restart_on_failure:
            cmd += ["-p", "Restart=on-failure", "-p", "RestartSec=5"]
        cmd += ["bash", script_path]

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if r.returncode != 0:
                return {"success": False,
                        "error": f"systemd-run fehlgeschlagen: {r.stderr.strip()[:300]}"}
        except Exception as e:
            return {"success": False, "error": f"Start-Fehler: {e}"}

        meta = {
            "task_id": tid, "unit": unit, "command": command,
            "description": description, "restart_on_failure": restart_on_failure,
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "running", "log_file": log_path}
        self._save_meta(tid, meta)

        # 3–5 s warten, dann Status melden
        time.sleep(self.initial_delay)
        info = self._query_status(tid)
        meta["status"] = info.get("status", "unknown")
        self._save_meta(tid, meta)

        return {
            "success": True, "task_id": tid, "unit": unit,
            "status": info.get("status"), "detail": info.get("detail", ""),
            "log_file": log_path,
            "hint": "Mit bg_task action=status task_id=<id> erneut abfragen."}

    def status(self, task_id: str) -> Dict[str, Any]:
        meta = self._load_meta(task_id)
        if not meta:
            return {"success": False, "error": f"Task {task_id} nicht gefunden"}
        info = self._query_status(task_id)
        meta["status"] = info.get("status", "unknown")
        self._save_meta(task_id, meta)
        return {
            "success": True, "task_id": task_id,
            "command": meta.get("command", ""),
            "status": info.get("status"), "detail": info.get("detail", ""),
            "exit_code": info.get("exit_code"),
            "log_file": self._log_path(task_id)}

    def list_tasks(self) -> Dict[str, Any]:
        tasks = []
        for fname in sorted(os.listdir(self.base_dir)):
            if not fname.endswith(".json"):
                continue
            tid = fname[:-5]
            meta = self._load_meta(tid)
            info = self._query_status(tid)
            tasks.append({
                "task_id": tid,
                "command": (meta.get("command", "") or "")[:80],
                "status": info.get("status"),
                "created_at": meta.get("created_at", "")})
        return {"success": True, "count": len(tasks), "tasks": tasks}

    def stop(self, task_id: str) -> Dict[str, Any]:
        meta = self._load_meta(task_id)
        if not meta:
            return {"success": False, "error": f"Task {task_id} nicht gefunden"}
        unit = meta.get("unit", self._unit_name(task_id))
        for sub in (["stop"], ["kill"]):
            try:
                subprocess.run(["systemctl", "--user"] + sub + [unit],
                               capture_output=True, timeout=15)
            except Exception:
                pass
        meta["status"] = "stopped"
        self._save_meta(task_id, meta)
        return {"success": True, "task_id": task_id, "status": "stopped"}

    def restart(self, task_id: str) -> Dict[str, Any]:
        meta = self._load_meta(task_id)
        if not meta:
            return {"success": False, "error": f"Task {task_id} nicht gefunden"}
        if not meta.get("command"):
            return {"success": False, "error": "Kein Command gespeichert – kein Restart möglich"}
        self.stop(task_id)
        self._remove_artifacts(task_id)
        return self.start(
            command=meta.get("command", ""),
            description=meta.get("description", ""),
            restart_on_failure=meta.get("restart_on_failure", False),
            task_id=task_id)

    def clean(self) -> Dict[str, Any]:
        """Entfernt alle abgeschlossenen/fehlgeschlagenen Tasks (läuft nicht mehr)."""
        removed = 0
        for fname in list(os.listdir(self.base_dir)):
            if not fname.endswith(".json"):
                continue
            tid = fname[:-5]
            info = self._query_status(tid)
            if info.get("status") in ("completed", "failed", "interrupted", "stopped", "unknown"):
                self._remove_artifacts(tid)
                try:
                    os.remove(self._meta_path(tid))
                    removed += 1
                except Exception:
                    pass
        return {"success": True, "removed": removed}
