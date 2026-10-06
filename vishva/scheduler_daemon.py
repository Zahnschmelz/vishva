#!/usr/bin/env python3
import os
import sys
import json
import time
import shutil
import shlex
import subprocess
import tempfile
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from .paths import p, cfg_path, BASE_DIR
from .scheduler import TaskScheduler, process_due_tasks, send_telegram_message
from .agent import AgentCore
from .rag_maintenance import RagMaintainer

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TMP_DIR = os.path.join(PROJECT_DIR, "tmp")
os.makedirs(TMP_DIR, exist_ok=True)

def load_config(path: str = None) -> dict:
    try:
        with open(path or p("config", "config.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}

def _as_list(value, default):
    if value is None:
        return list(default)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(x) for x in value if str(x).strip()]
    return list(default)

def cleanup_stale_task_scripts(
    max_age_hours: float = 24.0,
    project_dir: Optional[str] = None) -> int:
    directory = project_dir or p("tmp")
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    try:
        for name in os.listdir(directory):
            if not (name.startswith(".task_") and name.endswith(".sh")):
                continue
            path = os.path.join(directory, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
                    print(f"[DEBUG] Cleanup: Old script deleted: {name}")
            except Exception:
                pass
    except Exception as e:
        print(f"[DEBUG] Cleanup-Fehler: {e}")
    if removed:
        print(f"[DEBUG] Cleanup: {removed} old .task_*.sh removed.")
    return removed

def notify_send(
    config: dict,
    title: str,
    body: str,
    urgency: Optional[str] = None) -> bool:
    if not bool(config.get("scheduler_notifications_enabled", True)):
        print(f"[notify] deactivated | {title}: {str(body)[:120]}")
        return False
    command = str(
        config.get("scheduler_notification_command", "notify-send")
        or "notify-send").strip()
    try:
        timeout_ms = int(
            config.get("scheduler_notification_timeout_ms", 10000) or 10000)
    except Exception:
        timeout_ms = 10000
    app_name = str(
        config.get("scheduler_notification_app_name", "Vishva") or "Vishva")
    icon = str(config.get("scheduler_notification_icon", "") or "").strip()
    if urgency is None:
        urgency = str(
            config.get("scheduler_notification_urgency", "normal") or "normal")
    title = str(title or "")
    body = str(body or "")
    safe_title = shlex.quote(title)
    safe_body = shlex.quote(body)
    safe_urgency = shlex.quote(urgency)
    safe_icon = shlex.quote(icon)
    safe_app_name = shlex.quote(app_name)
    try:
        if command != "notify-send":
            if "{title}" in command or "{body}" in command:
                cmd = command.format(
                    title=safe_title,
                    body=safe_body,
                    urgency=safe_urgency,
                    timeout=timeout_ms,
                    icon=safe_icon,
                    app_name=safe_app_name,)
            else:
                cmd = f"{command} {safe_title} {safe_body}"
            subprocess.run(cmd, shell=True, timeout=10, check=False)
            return True
        if not shutil.which("notify-send"):
            print(f"[notify] notify-send not found | {title}: {body[:120]}")
            return False
        cmd = [
            "notify-send",
            "--app-name", app_name,
            "--expire-time", str(timeout_ms),
            "--urgency", urgency,]
        if icon:
            cmd.extend(["--icon", icon])
        cmd.extend([title, body])
        subprocess.run(cmd, timeout=10, check=False)
        return True
    except Exception as e:
        print(f"[notify] Error: {e}")
        return False

def play_alarm_sound(config: dict) -> bool:
    if not bool(config.get("scheduler_play_sound", True)):
        return False
    sound_files = _as_list(
        config.get("scheduler_sound_files"),
        ["alarm.mp3", "alarm.wav"],)
    sound_file = None
    for f in sound_files:
        f = str(f).strip()
        cand = f if os.path.isabs(f) else p(f)
        if cand and os.path.exists(cand):
            sound_file = cand
            break
    if not sound_file:
        print(f"[sound] Keine Sound-Datei gefunden: {sound_files}")
        return False
    safe_file = shlex.quote(sound_file)
    try:
        custom_cmd = str(config.get("scheduler_sound_command", "") or "").strip()
        if custom_cmd:
            if "{file}" in custom_cmd:
                cmd = custom_cmd.format(file=safe_file)
            else:
                cmd = f"{custom_cmd} {safe_file}"
            subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,)
            print(f"[sound] Custom-Player: {sound_file}")
            return True
        player = str(
            config.get("scheduler_sound_player", "auto") or "auto").lower()
        if player != "auto":
            parts = shlex.split(player)
            if not parts:
                return False
            if "{file}" in player:
                cmd = [p.replace("{file}", sound_file) for p in parts]
            else:
                cmd = parts + [sound_file]
            if shutil.which(parts[0]):
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,)
                print(f"[sound] Player: {parts[0]} | Datei: {sound_file}")
                return True
            print(f"[sound] Player nicht gefunden: {parts[0]}")
            return False
        ext = os.path.splitext(sound_file)[1].lower()
        candidates = []
        if ext == ".mp3":
            candidates = [
                ["mpg123", "-q", "{file}"],
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "{file}"],]
        elif ext == ".wav":
            candidates = [
                ["aplay", "-q", "{file}"],
                ["paplay", "{file}"],
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "{file}"],]
        elif ext == ".ogg":
            candidates = [
                ["paplay", "{file}"],
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "{file}"],]
        else:
            candidates = [
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "{file}"],]
        for candidate in candidates:
            if shutil.which(candidate[0]):
                cmd = [arg.replace("{file}", sound_file) for arg in candidate]
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,)
                print(f"[sound] Auto-Player: {candidate[0]} | Datei: {sound_file}")
                return True
        print("[sound] Kein geeigneter Audio-Player gefunden.")
        return False
    except Exception as e:
        print(f"[sound] Fehler: {e}")
        return False

def get_default_terminal() -> Optional[str]:
    terminal = os.environ.get("TERMINAL", "").strip()
    if terminal and shutil.which(terminal):
        return terminal
    for kreadconfig in ("kreadconfig5", "kreadconfig6"):
        try:
            result = subprocess.run(
                [kreadconfig, "--file", "kdeglobals", "--group", "General",
                 "--key", "TerminalApplication"],
                capture_output=True, text=True, timeout=5,)
            terminal = result.stdout.strip()
            if terminal and shutil.which(terminal):
                return terminal
        except Exception:
            pass
    try:
        result = subprocess.run(
            ["gsettings", "get",
             "org.gnome.desktop.default-applications.terminal", "exec"],
            capture_output=True, text=True, timeout=5,)
        terminal = result.stdout.strip().strip("'\"")
        if terminal and shutil.which(terminal):
            return terminal
    except Exception:
        pass
    if shutil.which("x-terminal-emulator"):
        return "x-terminal-emulator"
    for candidate in [
        "konsole", "gnome-terminal", "xfce4-terminal",
        "mate-terminal", "tilix", "alacritty", "kitty",
        "xterm", "urxvt", "lxterminal",]:
        if shutil.which(candidate):
            return candidate
    return None

def _build_terminal_cmd(terminal: str, title: str, script: str) -> List[str]:
    b = os.path.basename(terminal).lower()
    if b in ("gnome-terminal", "gnome-terminal-w", "ptyxis"):
        return [terminal, "--title", title, "--", script]
    if b == "kgx":
        return [terminal, "--", script]
    if b == "foot":
        return [terminal, "--title", title, script]
    if b in ("wezterm", "wezterm-gui"):
        return [terminal, "start", "--", script]
    if b in ("xterm", "urxvt", "rxvt", "rxvt-unicode", "aterm", "eterm", "mrxvt"):
        return [terminal, "-title", title, "-e", script]
    if b in ("st", "st-term", "xst"):
        return [terminal, "-T", title, "-e", script]
    return [terminal, "--title", title, "-e", script]

def open_terminal_with_file(
    filepath: str,
    title: str = "Vishva Scheduler",
    terminal_hint: str = ""):
    if terminal_hint == "telegram":
        print("[DEBUG] Hint=telegram → Do not open the terminal")
        return False
    project_dir = os.path.dirname(os.path.abspath(__file__))
    abs_filepath = os.path.abspath(filepath)
    print(f"[DEBUG] wait for file: {abs_filepath}")
    timeout = 300
    interval = 5
    elapsed = 0
    while not os.path.exists(abs_filepath) and elapsed < timeout:
        time.sleep(interval)
        elapsed += interval
        print(f"[DEBUG] ... {elapsed}s waited, file not available")
    if not os.path.exists(abs_filepath):
        print(f"[DEBUG] ❌ File after {timeout}s NOT found: {abs_filepath}")
        return False
    print(f"[DEBUG] ✅ File not found after {elapsed}s: {abs_filepath}")
    terminal = None
    if terminal_hint:
        terminal = shutil.which(terminal_hint)
        if terminal:
            print(f"[DEBUG] Terminal from task: {terminal}")
    if not terminal:
        terminal = get_default_terminal()
        print(f"[DEBUG] Terminal-Default: {terminal}")
    if not terminal:
        print("[DEBUG] Schritt 2: ❌ kein Terminal-Emulator gefunden")
        return False
    base = os.path.basename(abs_filepath)
    task_tag = base.replace(".txt", "")
    script_name = f".{task_tag}.sh"
    script_path = os.path.join(p("tmp"), script_name)
    rel_script = f"./tmp/{script_name}"
    script_content = f"""#!/bin/bash
trap 'rm -f "$0"' EXIT HUP INT TERM
cat "{abs_filepath}"
echo ""
echo "──────────────────────────────────────────────────────────"
echo ">>> press a key..."
read -n1 -s
"""
    try:
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script_content)
        print(f"[DEBUG] ✅ script created: {script_path}")
    except Exception as e:
        print(f"[DEBUG] ❌ error while creating: {e}")
        return False
    try:
        os.chmod(script_path, 0o755)
        print(f"[DEBUG] ✅ chmod +x: {script_path}")
    except Exception as e:
        print(f"[DEBUG] ❌ chmod-Fehler: {e}")
        return False

    cmd = _build_terminal_cmd(terminal, title, rel_script)
    print(f"[DEBUG] start Terminal: {cmd}")
    try:
        proc = subprocess.Popen(cmd, start_new_session=True, cwd=BASE_DIR)
        print(f"[DEBUG] ✅ Terminal started (PID {proc.pid})")
        return True
    except Exception as e:
        print(f"[DEBUG] ❌ Error: {e}")
        return False

def main():
    config = load_config()
    scheduler = TaskScheduler(config=config)
    os.makedirs(p("tmp"), exist_ok=True)

    if not scheduler.enabled:
        print("❌ Scheduler ist deaktiviert (scheduler_enabled=false).")
        sys.exit(0)
    if not scheduler.daemon_enabled:
        print("❌ Scheduler-Daemon ist deaktiviert (scheduler_daemon_enabled=false).")
        sys.exit(0)

    print("⏰ Vishva Scheduler Daemon gestartet")
    print(f"   interval: {scheduler.check_interval}s")
    print(f"   Daemon-Targets: {', '.join(scheduler.daemon_targets)}")

    try:
        # Dedizierte session_id, damit der Daemon NICHT die neueste
        # User-Session lädt/berührt. Da "scheduler_daemon.json" nicht
        # existiert, legt create_session() eine frische, isolierte
        # Session mit zufälliger ID an.
        agent = AgentCore(
            session_id="scheduler_daemon",
            enable_tts=False,
        )

        agent.tool_manager.scheduler = scheduler
        agent.interface = "daemon"

        # RAG-Maintenance
        rag_maintainer = None
        if agent.rag_manager and bool(config.get("rag_maintenance_enabled", True)):
            rag_maintainer = RagMaintainer(
                rag_manager=agent.rag_manager,
                config=config,
                daemon_ref=None,
                # WICHTIG: die Daemon-eigene Session im Idle-Gate ignorieren,
                # sonst blockiert die gerade angelegte Session-Datei
                # (mtime = jetzt) die Wartung für immer.
                ignore_session_id=agent.get_session_id())
            print(f"[Daemon] RAG-Maintenance initialisiert "
                  f"(ignore_session={agent.get_session_id()}).")
    except Exception as e:
        print(f"❌ AgentCore konnte nicht gestartet werden: {e}")
        sys.exit(1)

    try:
        res = agent.session_manager.cleanup_empty_sessions(
            protect_session_id=agent.get_session_id())
        if res.get("removed"):
            print(f"[Daemon] Session-Cleanup: {res['removed']} leere Sessions entfernt")
    except Exception as e:
        print(f"[Daemon] Session-Cleanup-Fehler: {e}")

    # Embedding-Modell vorwärmen, damit der erste Maintenance-Tick
    # es nicht erst laden muss. (Wird von rag_preload_model bereits
    # als Thread angestoßen; der Lock verhindert Doppel-Laden.)
    try:
        if getattr(agent, "rag_manager", None):
            agent.rag_manager._embed("vishva scheduler warmup")
    except Exception:
        pass

    def pre_chat(task: dict):
        if bool(scheduler.config.get("scheduler_clear_session_per_task", True)):
            try:
                agent.clear_current()
            except Exception:
                pass

    def notify_send_cb(response: str, task: dict):
        cfg = scheduler.config
        task_id = task.get("id", "?")
        prompt = str(task.get("prompt", ""))[:120]
        response_str = str(response or "")
        is_error = response_str.strip().startswith("❌")
        prefix = str(
            cfg.get("scheduler_notification_title_prefix", "Vishva") or "Vishva")
        if is_error:
            title = f"{prefix} Task {task_id} failed"
        else:
            title = f"{prefix} Task {task_id} done"
        body = response_str or "(Leere Antwort)"
        try:
            max_len = int(
                cfg.get("scheduler_notification_max_chars", 500) or 500)
        except Exception:
            max_len = 500
        log_dir = cfg_path(cfg, "scheduler_notify_log_dir", "scheduler_notifications")
        log_path = ""
        if log_dir:
            try:
                os.makedirs(log_dir, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                log_path = os.path.join(log_dir, f"task_{task_id}_{ts}.txt")
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(f"Task ID:    {task_id}\n")
                    f.write(f"Target:     {task.get('target', '')}\n")
                    f.write(f"Status:     {'error' if is_error else 'done'}\n")
                    f.write(f"Trigger:    {task.get('trigger_time', '')}\n")
                    f.write(f"Prompt:     {task.get('prompt', '')}\n")
                    f.write(f"{'─' * 60}\n\n")
                    f.write(f"Answer:\n\n{response_str}\n")
            except Exception as e:
                print(f"[Scheduler] Log-Failure: {e}")
                log_path = ""
        if len(body) > max_len:
            body = body[:max_len] + "…"
            if log_path:
                body += f"\n\nfull answer:\n{log_path}"
        body = f"Aufgabe: {prompt}\n\n{body}"
        if is_error:
            urgency = str(
                cfg.get("scheduler_notification_error_urgency", "critical")
                or "critical")
        else:
            urgency = str(
                cfg.get("scheduler_notification_urgency", "normal") or "normal")
        notify_send(cfg, title, body, urgency=urgency)
        if is_error:
            if bool(cfg.get("scheduler_play_sound_on_error", True)):
                play_alarm_sound(cfg)
        else:
            if bool(cfg.get("scheduler_play_sound", True)):
                play_alarm_sound(cfg)
        hint = str(task.get("terminal", "") or "")
        if hint == "telegram":
            bot_token, chat_id = scheduler.telegram_credentials(task)
            if bot_token and chat_id:
                full_text = (
                    f"⏰ Task {task_id} done\n"
                    f"Prompt: {prompt}\n\n"
                    f"{response_str}")
                send_telegram_message(full_text, bot_token, chat_id)
                print(f"[Terminal] Hint=telegram → full answer via Telegram")
            else:
                print("[Terminal] Hint=telegram, but no TG-Credentials")
        elif (
            log_path
            and not is_error
            and bool(cfg.get("scheduler_open_terminal", True))):
            terminal_title = str(
                cfg.get("scheduler_terminal_title", f"{prefix} Scheduler answer")
                or f"{prefix} Scheduler answer")
            open_terminal_with_file(
                log_path,
                title=terminal_title,
                terminal_hint=hint,)
    project_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        script_max_age = float(
            config.get("scheduler_script_cleanup_max_age_hours", 24) or 24)
    except Exception:
        script_max_age = 24.0
    try:
        script_cleanup_interval = int(
            config.get("scheduler_script_cleanup_interval", 3600) or 3600)
    except Exception:
        script_cleanup_interval = 3600
    cleanup_stale_task_scripts(
        max_age_hours=script_max_age,
        project_dir=project_dir,)
    last_script_cleanup = time.time()
    last_session_cleanup = time.time()
    first_run = True
    while True:
        try:
            if not first_run:
                time.sleep(scheduler.check_interval)
            first_run = False
            if time.time() - last_session_cleanup >= 3600:
                last_session_cleanup = time.time()
                try:
                    res = agent.session_manager.cleanup_empty_sessions(
                        protect_session_id=agent.get_session_id())
                    if res.get("removed"):
                        print(f"[Daemon] Session-Cleanup: {res['removed']} leere Sessions entfernt")
                except Exception:
                    pass
            # RAG-Maintenance Tick
            if rag_maintainer:
                try:
                    rag_maintainer.tick()
                except Exception as e:
                    print(f"[Daemon] RAG-Maintenance Fehler: {e}")
            if hasattr(scheduler, "reload"):
                scheduler.reload()
            else:
                scheduler._load()
            due = scheduler.get_due_tasks()
            print(
                f"[Scheduler] {datetime.now().strftime('%H:%M:%S')} | "
                f"Tasks: {len(scheduler.tasks)} | "
                f"Due: {len(due)}")
            process_due_tasks(
                agent,
                scheduler,
                current_target="daemon",
                notify_send=notify_send_cb,
                pre_chat=pre_chat,)
            now_ts = time.time()
            if now_ts - last_script_cleanup >= script_cleanup_interval:
                cleanup_stale_task_scripts(
                    max_age_hours=script_max_age,
                    project_dir=project_dir,)
                last_script_cleanup = now_ts
        except KeyboardInterrupt:
            print("⏹️ Scheduler Daemon closed.")
            break
        except Exception as e:
            print(f"❌ Scheduler-Loop-Failure: {e}")
            notify_send(
                scheduler.config,
                "Vishva Scheduler Error",
                str(e)[:500],
                urgency=str(
                    scheduler.config.get(
                        "scheduler_notification_error_urgency", "critical")
                    or "critical"),)

if __name__ == "__main__":
    main()
