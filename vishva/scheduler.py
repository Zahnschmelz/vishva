import os
import json
import uuid
import re
import shutil
import threading
from datetime import datetime
from typing import Dict, List, Any, Optional, Callable
from .paths import p

VALID_TARGETS = {"telegram", "cli", "gui", "auto"}

TERMINAL_PROCESSES = [
    ("gnome-terminal", ["gnome-terminal-w", "gnome-terminal-"]),
    ("kgx", ["kgx"]),
    ("konsole", ["konsole"]),
    ("xfce4-terminal", ["xfce4-terminal"]),
    ("lxterminal", ["lxterminal"]),
    ("mate-terminal", ["mate-terminal"]),
    ("qterminal", ["qterminal"]),
    ("tilix", ["tilix"]),
    ("terminator", ["terminator"]),
    ("alacritty", ["alacritty"]),
    ("kitty", ["kitty"]),
    ("wezterm", ["wezterm-gui", "wezterm"]),
    ("ghostty", ["ghostty"]),
    ("foot", ["foot"]),
    ("wayst", ["wayst"]),
    ("wayle", ["wayle"]),
    ("dwlterm", ["dwlterm"]),
    ("contour", ["contour"]),
    ("blackbox", ["blackbox", "com.raggesilver.blackbox"]),
    ("ptyxis", ["ptyxis"]),
    ("sakura", ["sakura"]),
    ("roxterm", ["roxterm"]),
    ("lilyterm", ["lilyterm"]),
    ("terminology", ["terminology"]),
    ("guake", ["guake"]),
    ("yakuake", ["yakuake"]),
    ("tilda", ["tilda"]),
    ("quaketerminal", ["quaketerminal"]),
    ("tabby", ["tabby"]),
    ("hyper", ["hyper"]),
    ("xterm", ["xterm"]),
    ("rxvt-unicode", ["rxvt-unicode"]),
    ("urxvt", ["urxvt"]),
    ("rxvt", ["rxvt"]),
    ("aterm", ["aterm"]),
    ("eterm", ["eterm"]),
    ("mlterm", ["mlterm"]),
    ("mrxvt", ["mrxvt"]),
    ("st-term", ["st-term"]),
    ("xst", ["xst"]),
    ("st", ["st"]),
    ("pangoterm", ["pangoterm"]),
    ("termite", ["termite"]),
    ("termit", ["termit"]),
    ("finalterm", ["finalterm", "final-term"]),
    ("evilvte", ["evilvte"]),
    ("pterm", ["pterm"]),
    ("cool-retro-term", ["cool-retro-term", "cool-retro-ter"]),]

def _pstree_chain() -> str:
    try:
        r = subprocess.run(
            ["pstree", "-s", str(os.getpid())],
            capture_output=True, text=True, timeout=5,)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass

    names = []
    pid = os.getpid()
    for _ in range(40):
        try:
            with open(f"/proc/{pid}/stat", "r") as f:
                data = f.read()
            close = data.rfind(")")
            comm = data[data.find("(") + 1:close]
            ppid = int(data[close + 2:].split()[1])
            names.append(comm)
            if ppid <= 1:
                break
            pid = ppid
        except Exception:
            break
    return "---".join(names)

def detect_terminal_via_pstree() -> Optional[str]:
    chain = _pstree_chain().lower()
    tokens = [t for t in re.split(r"[^a-z0-9_.-]+", chain) if t]
    for canonical, procs in TERMINAL_PROCESSES:
        for proc in procs:
            needle = proc[:15]
            for tok in tokens:
                if tok == needle:
                    if shutil.which(canonical):
                        return canonical
                    if shutil.which(proc):
                        return proc
    return None

class TaskScheduler:
    def __init__(
        self,
        path: str = None,
        config: Optional[Dict[str, Any]] = None,
        config_path: str = None):
        self.path = path or p("data", "scheduled_tasks.json")
        self.config = config if config is not None else self._load_config(
            config_path or p("config", "config.json"))
        self.tasks: List[Dict[str, Any]] = []
        self._lock = threading.RLock()
        self._load()

# Basics
    @staticmethod
    def _load_config(path: str) -> Dict[str, Any]:
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now()

    @staticmethod
    def _parse_time(t: str) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    @property
    def fallback_delay(self) -> int:
        try:
            return max(0, int(self.config.get("scheduler_fallback_delay", 300) or 300))
        except Exception:
            return 300

    def resolve_fallback(self, task: Dict[str, Any]) -> Optional[str]:
        fb = str(task.get("fallback_target", "") or "").lower()

        if fb in ("none", "off", "false", ""):
            if fb in ("none", "off", "false"):
                return None
        elif fb in ("telegram", "cli", "gui", "auto"):
            return fb
        target = self.resolve_target(task)
        fallbacks = self.config.get("scheduler_default_fallbacks", {})
        if isinstance(fallbacks, dict):
            fb = str(fallbacks.get(target, "") or "").lower()
        if not fb:
            fb = str(self.config.get("scheduler_default_fallback_target", "telegram") or "telegram").lower()
        if fb in ("none", "off", "false"):
            return None
        if fb in ("telegram", "cli", "gui", "auto"):
            return fb
        return None

    def get_delivery_mode(self, task: Dict[str, Any], current_target: str) -> Optional[str]:
        current_target = str(current_target or "").lower()
        if not current_target:
            return None

        if current_target == "daemon":
            if not self.daemon_enabled:
                return None
            target = self.resolve_target(task)
            if target in self.daemon_targets:
                return "primary"
            return None
        if self.daemon_enabled:
            target = self.resolve_target(task)
            if target in self.daemon_targets:
                return None
        target = self.resolve_target(task)
        fallback = self.resolve_fallback(task)
        if target == "auto":
            if current_target in ("cli", "gui", "telegram"):
                return "primary"
            return None
        if target == current_target:
            return "primary"
        if fallback:
            if fallback == current_target or fallback == "auto":
                trigger = self._parse_time(task.get("trigger_time", ""))
                if trigger:
                    try:
                        age = (self._now() - trigger).total_seconds()
                    except Exception:
                        age = self.fallback_delay + 1
                    if age >= self.fallback_delay:
                        return "fallback"
        return None

    def _load(self):
        with self._lock:
            if not os.path.exists(self.path):
                self.tasks = []
                return
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self.tasks = [self._normalize_task(t) for t in data]
                else:
                    self.tasks = []
            except Exception:
                try:
                    os.rename(self.path, self.path + ".corrupt")
                except Exception:
                    pass
                self.tasks = []

    def _save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.tasks, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)

    def _normalize_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(task, dict):
            task = {}
        task.setdefault("id", str(uuid.uuid4())[:8])
        task.setdefault("prompt", "")
        task.setdefault("trigger_time", "")
        task.setdefault("status", "pending")
        task.setdefault("target", "")
        task.setdefault("terminal", "")
        task.setdefault("fallback_target", "")
        task.setdefault("chat_id", "")
        task.setdefault("created_at", self._now().isoformat())
        return task

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("scheduler_enabled", True))

    @property
    def check_interval(self) -> int:
        try:
            return max(5, int(self.config.get("scheduler_check_interval", 30) or 30))
        except Exception:
            return 30

    @property
    def claim_ttl(self) -> int:
        try:
            return max(60, int(self.config.get("scheduler_claim_ttl", 900) or 900))
        except Exception:
            return 900

    @property
    def default_target(self) -> str:
        target = str(self.config.get("scheduler_default_target", "auto") or "auto").lower()
        if target not in VALID_TARGETS:
            return "auto"
        return target

    @property
    def default_chat_id(self) -> str:
        return str(
            self.config.get("scheduler_default_chat_id", "")
            or self.config.get("chat_id", "")
            or "")

    @property
    def daemon_enabled(self) -> bool:
        return bool(self.config.get("scheduler_daemon_enabled", False))

    @property
    def daemon_targets(self) -> List[str]:
        raw = self.config.get("scheduler_daemon_targets", ["cli", "gui"])
        if not isinstance(raw, list):
            raw = ["cli", "gui"]
        out = []
        for t in raw:
            t = str(t).lower()
            if t in VALID_TARGETS:
                out.append(t)
        return out

    def telegram_credentials(self, task: Optional[Dict[str, Any]] = None):
        bot_token = str(self.config.get("bot_token", "") or "")
        chat_id = ""
        if task:
            chat_id = str(task.get("chat_id", "") or "")
        if not chat_id:
            chat_id = self.default_chat_id
        return bot_token, chat_id

    def resolve_target(self, task: Dict[str, Any]) -> str:
        target = str(task.get("target", "") or self.default_target).lower()
        if target not in VALID_TARGETS:
            return "auto"
        return target

    def add_task(
        self,
        chat_id: Optional[str],
        trigger_time: str,
        prompt: str,
        target: Optional[str] = None,
        fallback_target: Optional[str] = None,
        terminal: Optional[str] = None) -> Dict[str, Any]:
        try:
            dt = datetime.fromisoformat(str(trigger_time).replace("Z", "+00:00"))
        except ValueError:
            return {
                "error": "Invalid date. Format: YYYY-MM-DD HH:MM:SS"}

        if dt < self._now():
            return {
                "error": "The time is in the past."}

        prompt = (prompt or "").strip()
        if not prompt:
            return {
                "error": "prompt is empty."}
        resolved_target = str(target or self.default_target).lower()
        if resolved_target not in VALID_TARGETS:
            return {
                "error": f"invalid target: {resolved_target}. allowed: {', '.join(sorted(VALID_TARGETS))}"}
        task_id = str(uuid.uuid4())[:8]
        task = {
            "id": task_id,
            "chat_id": str(chat_id or ""),
            "trigger_time": trigger_time,
            "prompt": prompt,
            "target": resolved_target,
            "fallback_target": str(fallback_target or ""),
            "terminal": terminal or detect_terminal_via_pstree() or "",
            "status": "pending",
            "created_at": self._now().isoformat()}

        with self._lock:
            self._load()
            self.tasks.append(self._normalize_task(task))
            self._save()
        print(f"[Scheduler] Task {task_id} | Terminal: {task['terminal'] or '(no terminal)'}")
        return {
            "success": True,
            "task_id": task_id,
            "target": resolved_target,
            "message": f"Task {task_id} geplant für {trigger_time} → {resolved_target}"}

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        with self._lock:
            self._load()
            original_len = len(self.tasks)
            self.tasks = [
                t for t in self.tasks
                if t.get("id") != task_id]
            if len(self.tasks) < original_len:
                self._save()
                return {
                    "success": True,
                    "message": f"Task {task_id} abgebrochen."}
        return {
            "error": f"Task {task_id} nicht gefunden."}

    def list_tasks(self, chat_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            self._load()
            if chat_id:
                return [
                    t for t in self.tasks
                    if t.get("chat_id") == chat_id]
            return list(self.tasks)

    def recover_stale_tasks(self):
        now = self._now()
        ttl = self.claim_ttl
        changed = False
        with self._lock:
            for task in self.tasks:
                if task.get("status") != "running":
                    continue
                claimed_at = self._parse_time(task.get("claimed_at", ""))
                if not claimed_at:
                    task["status"] = "pending"
                    task.pop("claimed_by", None)
                    task.pop("claimed_at", None)
                    changed = True
                    continue
                try:
                    age = (now - claimed_at).total_seconds()
                except Exception:
                    age = ttl + 1
                if age > ttl:
                    task["status"] = "pending"
                    task.pop("claimed_by", None)
                    task.pop("claimed_at", None)
                    changed = True
            if changed:
                self._save()

    def reload(self):
        try:
            self._load()
        except Exception as e:
            print(f"[Scheduler] reload-failor: {e}")

    def get_due_tasks(self) -> List[Dict[str, Any]]:
        self.reload()
        self.recover_stale_tasks()
        now = self._now()
        due: List[Dict[str, Any]] = []
        for task in list(self.tasks):
            if task.get("status") != "pending":
                continue
            trigger = self._parse_time(
                task.get("trigger_time") or task.get("next_trigger", ""))
            if trigger and trigger <= now:
                due.append(task)
        return due

    def claim_task(self, task_id: str, claimant: str) -> bool:
        with self._lock:
            self._load()
            for task in self.tasks:
                if task.get("id") != task_id:
                    continue
                if task.get("status") != "pending":
                    return False
                task["status"] = "running"
                task["claimed_by"] = claimant
                task["claimed_at"] = self._now().isoformat()
                self._save()
                return True
        return False

    def mark_done(self, task_id: str, delivered_via: str = "") -> bool:
        with self._lock:
            self._load()
            for task in self.tasks:
                if task.get("id") != task_id:
                    continue
                task["status"] = "done"
                task["delivered_via"] = delivered_via
                task["delivered_at"] = self._now().isoformat()
                task.pop("claimed_by", None)
                task.pop("claimed_at", None)
                self._save()
                return True
        return False

    def mark_failed(self, task_id: str, error: str) -> bool:
        with self._lock:
            self._load()
            for task in self.tasks:
                if task.get("id") != task_id:
                    continue
                task["status"] = "failed"
                task["last_error"] = error
                task["failed_at"] = self._now().isoformat()
                task.pop("claimed_by", None)
                task.pop("claimed_at", None)
                self._save()
                return True
        return False

def _chunk_text(text: str, size: int = 4000) -> List[str]:
    text = str(text or "")
    if not text:
        return [""]
    return [
        text[i:i + size]
        for i in range(0, len(text), size)]

def send_telegram_message(text: str, bot_token: str, chat_id: str) -> bool:
    if not bot_token or not chat_id:
        return False
    try:
        import requests
    except ImportError:
        print("❌ requests is missing for sending messages via Telegram")
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        for chunk in _chunk_text(text, 4000):
            payload = {
                "chat_id": chat_id,
                "text": chunk}
            response = requests.post(url, json=payload, timeout=30)
            if not response.ok:
                print(f"❌ Telegram send failed: {response.status_code} {response.text[:200]}")
                return False
        return True
    except Exception as e:
        print(f"❌ Telegram send error: {e}")
        return False

def prepare_agent_for_scheduled_task(agent, scheduler: "TaskScheduler"):
    tm = getattr(agent, "tool_manager", None)
    if tm is None:
        return
    if bool(scheduler.config.get("scheduler_reload_tools", True)):
        if hasattr(tm, "_load_or_init"):
            try:
                tm._load_or_init()
            except Exception as e:
                print(f"[Scheduler] tool-reload-error: {e}")
    required_tools = scheduler.config.get("scheduler_required_tools", []) or []
    if required_tools and hasattr(tm, "active_tools"):
        try:
            tm.active_tools.update(str(t) for t in required_tools)
        except Exception as e:
            print(f"[Scheduler] required-tools-error: {e}")
    if bool(scheduler.config.get("scheduler_refresh_system_prompt", True)):
        if hasattr(agent, "_refresh_system_prompt"):
            try:
                agent._refresh_system_prompt()
            except Exception as e:
                print(f"[Scheduler] system-prompt-refresh-error: {e}")
    try:
        active_tools = sorted(getattr(tm, "active_tools", set()))
        print(f"[Scheduler] active tools for task: {len(active_tools)}")
    except Exception:
        pass

def process_due_tasks(
    agent,
    scheduler: TaskScheduler,
    current_target: str = "cli",
    local_send: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    telegram_send: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    notify_send: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    pre_chat: Optional[Callable[[Dict[str, Any]], None]] = None) -> List[Dict[str, Any]]:
    processed: List[Dict[str, Any]] = []
    if not scheduler.enabled:
        return processed
    current_target = str(current_target or "cli").lower()
    scheduler.recover_stale_tasks()
    for task in scheduler.get_due_tasks():
        task_id = task.get("id", "?")
        prompt = (task.get("prompt") or "").strip()
        if not prompt:
            scheduler.mark_failed(task_id, "Prompt ist leer.")
            continue
        delivery_mode = scheduler.get_delivery_mode(task, current_target)
        if not delivery_mode:
            continue
        if current_target not in ("telegram", "daemon") and local_send is None:
            continue
        if not scheduler.claim_task(task_id, current_target):
            continue
        try:
            if pre_chat:
                try:
                    pre_chat(task)
                except Exception as e:
                    print(f"[Scheduler] pre_chat failor: {e}")
            prep = globals().get("prepare_agent_for_scheduled_task")
            if prep:
                try:
                    prep(agent, scheduler)
                except Exception as e:
                    print(f"[Scheduler] prepare_agent failor: {e}")
            response = agent.chat(prompt)
            response = response or "(Empty Answer)"
            if current_target == "daemon":
                if notify_send:
                    notify_send(response, task)
                else:
                    print(f"[Scheduler] Task {task_id}: {response}")
                via = "notify"
            elif current_target == "telegram":
                if telegram_send:
                    telegram_send(response, task)
                else:
                    bot_token, chat_id = scheduler.telegram_credentials(task)
                    ok = send_telegram_message(response, bot_token, chat_id)
                    if not ok:
                        raise RuntimeError("Telegram delivery failed.")
                via = f"telegram:{delivery_mode}"
            else:
                if local_send:
                    local_send(response, task)
                via = f"local:{current_target}:{delivery_mode}"
            scheduler.mark_done(task_id, delivered_via=via)
            processed.append({
                "task": task,
                "response": response,
                "via": via,
                "mode": delivery_mode})
        except Exception as e:
            scheduler.mark_failed(task_id, str(e))
            if current_target == "daemon" and notify_send:
                try:
                    notify_send(f"❌ Error: {e}", task)
                except Exception:
                    pass

    return processed
