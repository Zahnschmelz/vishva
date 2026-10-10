import os
#import re
#import ast
import sys
import json
#import time
#import shlex
import shutil
#import tempfile
#import subprocess
from pathlib import Path
from rich.text import Text
from rich.panel import Panel
from rich.console import Console
from .scheduler import TaskScheduler
from .paths import p, cfg_path, BASE_DIR
from .background_tasks import BackgroundTaskManager
from typing import Dict, List, Any, Optional, Tuple

class ToolManager:
    def __init__(self, toolpool_path: str = None, activetools_path: str = None, tts_manager=None, config=None, exclude_tools: set = None):
        self.toolpool_path = toolpool_path or self._default_toolpool_path()
        self.activetools_path = activetools_path or p("config", "activetools.txt")
        self.tts_manager = tts_manager
        self.config = config or {}
        self.workdir = os.path.abspath(self.config.get("agent_workdir", "working_dir"))
        self.agent_cwd = self.workdir
        try:
            os.makedirs(self.workdir, exist_ok=True)
        except Exception:
            pass
        self.exclude_tools = exclude_tools or set()
        self.scheduler = TaskScheduler()
        self.available_tools: List[Dict[str, Any]] = []
        self.active_tools: set = set()
        self._load_or_init()
        self.mcp = None
        if bool(self.config.get("mcp_enabled", True)):
            try:
                import atexit
                from .mcp_client import MCPManager
                self.mcp = MCPManager(self.config)
                atexit.register(self.mcp.shutdown)
            except Exception as e:
                print(f"[ToolManager] MCP init failed: {e}")
                self.mcp = None
        self._load_mcp_tools()
        self._save_activetools()
        self.bg_manager = BackgroundTaskManager(self.config)
        self.confirm_fn = None
        self._session_approved = set()
        self._confirm_console = Console(soft_wrap=True)
        drift = self.validate_registry()
        if any(drift.values()):
            print(f"[ToolManager] Registry-Drift: {drift}")

    def _validate_path(self, path: str, allow_write: bool = True) -> Tuple[bool, Optional[str]]:
        if not path:
            return False, "path is required."
        try:
            abs_path = os.path.abspath(path)
            cwd = os.path.abspath(os.getcwd())
        except Exception as e:
            return False, f"Invalid path: {e}"
        dangerous = list(self.config.get("dangerous_paths", [
            "/etc/passwd", "/etc/shadow", "/etc/sudoers", "/root/", "/boot/",
            "`", "$(", "${"
        ]))
        for d in dangerous:
            if d in path:
                return False, f"Path contains dangerous pattern: '{d}'"
        if allow_write:
            parent = os.path.dirname(abs_path) or "."
            if os.path.exists(parent) and not os.access(parent, os.W_OK):
                return False, f"Directory not writable: {parent}"
        return True, None

    def reset_cwd(self):
        self.agent_cwd = self.workdir

    def _resolve_path(self, path: str) -> str:
        if not path or os.path.isabs(path):
            return os.path.normpath(path) if path else path
        return os.path.normpath(os.path.join(self.agent_cwd, path))

    def _resolve_venv(self) -> Optional[str]:
        candidates = []
        configured = self.config.get("venv_path")
        if configured:
            if not os.path.isabs(configured):
                configured = p(configured)
            candidates.append(configured)
        try:
            entries = [e for e in os.listdir(self.workdir) if "venv" in e.lower()]
            entries.sort(key=lambda e: (e.lower() != "venv", e.lower() != ".venv", e.lower()))
            candidates.extend(os.path.join(self.workdir, e) for e in entries)
        except Exception:
            pass
        for cand in candidates:
            if os.path.isfile(os.path.join(cand, "bin", "activate")):
                return os.path.normpath(cand)
        return None

    def _backup_file(self, path: str) -> Optional[str]:
        if not os.path.isfile(path):
            return None
        max_backups = int(self.config.get("max_backups", 20) or 0)
        if max_backups <= 0:
            return None
        backup_dir = cfg_path(self.config, "backup_dir", "backups")
        if not os.path.isabs(backup_dir):
            backup_dir = os.path.normpath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), backup_dir))
        try:
            abs_path = os.path.abspath(path)
            # Relativ zu BASE_DIR wenn möglich, sonst absoluter Pfad als Struktur
            try:
                rel = os.path.relpath(abs_path, BASE_DIR)
                if rel.startswith(".."):
                    # Außerhalb BASE_DIR: Pfadstruktur beibehalten
                    rel = abs_path.lstrip("/")
            except ValueError:
                rel = abs_path.lstrip("/")
            backup_path = os.path.join(backup_dir, rel)
            os.makedirs(os.path.dirname(backup_path) or backup_dir, exist_ok=True)
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            final = f"{backup_path}.{ts}.bak"
            shutil.copy2(path, final)
            d = os.path.dirname(final)
            prefix = os.path.basename(rel) + "."
            siblings = sorted(f for f in os.listdir(d)
                              if f.startswith(prefix) and f.endswith(".bak"))
            for old in siblings[:-max_backups] if len(siblings) > max_backups else []:
                try:
                    os.remove(os.path.join(d, old))
                except Exception:
                    pass
            return final
        except Exception:
            return None

    @staticmethod
    def _default_toolpool_path() -> str:
        toolpath = p("config", "toolpool.json")
        return toolpath

    def _load_or_init(self):
        if os.path.exists(self.toolpool_path):
            with open(self.toolpool_path, "r", encoding="utf-8") as f:
                self.available_tools = json.load(f)
        else:
            self.available_tools = []
            self._save_toolpool()
        if os.path.exists(self.activetools_path):
            with open(self.activetools_path, "r", encoding="utf-8") as f:
                self.active_tools = {line.strip() for line in f if line.strip()}
        else:
            self.active_tools = set()
            self._save_activetools()

    def _load_mcp_tools(self):
        if not self.mcp:
            return
        try:
            schemas = self.mcp.refresh_tools()
        except Exception as e:
            print(f"[ToolManager] MCP refresh failed: {e}")
            return
        existing = {t.get("function", {}).get("name")
                    for t in self.available_tools}
        auto = bool(self.config.get("mcp_auto_activate", True))
        for schema in schemas:
            name = schema["function"]["name"]
            if name not in existing:
                self.available_tools.append(schema)
                existing.add(name)
            if auto and name not in self.active_tools:
                if isinstance(self.active_tools, set):
                    self.active_tools.add(name)
                else:
                    self.active_tools.append(name)
        if schemas:
            print(f"[ToolManager] {len(schemas)} MCP-Tool(s) registriert")

    def _save_toolpool(self):
        tools = [t for t in self.available_tools
                 if not t.get("function", {}).get("name", "")
                        .startswith("mcp__")]
        with open(self.toolpool_path, "w", encoding="utf-8") as f:
            json.dump(tools, f, indent=2, ensure_ascii=False)

    def _save_activetools(self):
        names = [n for n in self.active_tools if not n.startswith("mcp__")]
        with open(self.activetools_path, "w", encoding="utf-8") as f:
            for tool in sorted(names):
                f.write(f"{tool}\n")

    def get_active_tools(self) -> List[Dict[str, Any]]:
        active = []
        for tool in self.available_tools:
            name = tool.get("function", {}).get("name", "")
            if name in self.active_tools and name not in self.exclude_tools:
                active.append(tool)
        return active

    _HANDLERS: Dict[str, str] = {
        "write_file": "_write_file",
        "read_file": "_read_file",
        "edit_file": "_edit_file",
        "cd": "_cd",
        "sys_update": "_sys_update",
        "sys_clean": "_sys_clean",
        "read_cache": "_read_cache",
        "web_search": "_web_search",
        "web_read": "_web_read",
        "product_search": "_product_search",
        "speak": "_speak",
        "sched_task": "_sched_task",
        "ls_tasks": "_ls_tasks",
        "cancel_task": "_cancel_task",
        "spotify": "_spotify",
        "vol_ctl": "_vol_ctl",
        "turnoff_screen": "_turnoff_screen",
        "subagent": "_subagent",
        "bash": "_bash",
        "bg_task": "_bg_task",
        "list_dir": "_list_dir",
        "weather": "_weather",
        "tarot": "_tarot",
        "news_digest": "_news_digest",
        "brightness_ctl": "_brightness_ctl",
        "send_image": "_send_image",
        "lint_code": "_lint_code",
        "send_file": "_send_file",
        "rag_save": "_rag_save",
        "rag_search": "_rag_search",
        "rag_reindex": "_rag_reindex",
        "rag_essential": "_rag_essential",
        "rag_update": "_rag_update",
        "rag_delete": "_rag_delete",}

    def reset_tool_counter(self):
        """Setzt den Tool-Call-Zähler zurück (Aufruf pro Chat-Turn)."""
        self.tool_call_count = 0

    def execute_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name in self.exclude_tools:
            return {"error": f"Tool '{name}' is not available in this environment."}
        if self._needs_confirmation(name):
            verdict = self._ask_confirmation(name, arguments or {})
            if not verdict.get("allow"):
                reason = verdict.get("reason") or "no reason given"
                return {
                    "error": f"Tool '{name}' wurde vom Nutzer abgelehnt. Grund: {reason}",
                    "denied": True,
                    "denied_reason": reason,
                }
        if name.startswith("mcp__"):
            if not self.mcp:
                return {"error": "MCP disabled (mcp_enabled=false or init failed)"}
            return self.mcp.call(name, arguments)
        handler_name = self._HANDLERS.get(name)
        if not handler_name:
            return {"error": f"Unknown tool: {name}."}
        try:
            return getattr(self, handler_name)(arguments or {})
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "tool": name}

    def set_confirm_handler(self, fn):
        """Interface-Handler: fn(tool_name, args_preview) -> dict
           {'allow': bool, 'reason': str, 'always': bool}"""
        self.confirm_fn = fn

    def _build_args_preview(self, arguments: Dict[str, Any], max_len: int = 0) -> str:
        if max_len <= 0:
            try:
                max_len = int(self.config.get("tool_confirm_preview_max", 100))
            except (TypeError, ValueError):
                max_len = 100
        try:
            s = json.dumps(arguments, ensure_ascii=False)
        except Exception:
            s = str(arguments)
        return s if len(s) <= max_len else s[:max_len] + "…"

    def _needs_confirmation(self, name: str) -> bool:
        import fnmatch
        if not bool(self.config.get("tool_confirm_enabled", True)):
            return False
        if name in self._session_approved:
            return False
        patterns = self.config.get("tool_confirm_tools", []) or []
        return any(fnmatch.fnmatch(name, pat) for pat in patterns)

    def _clear_lines(self, n: int):
        """Entfernt n Zeilen rückwärts via ANSI — nur auf echtem TTY wirksam."""
        if n <= 0:
            return
        try:
            if not (sys.stdout.isatty() if hasattr(sys.stdout, "isatty") else False):
                return
        except Exception:
            return
        sys.stdout.write(f"\033[{n}F")             # Cursor n Zeilen hoch
        for _ in range(n):
            sys.stdout.write("\033[2K\033[1B")     # Zeile leeren, eine runter
        sys.stdout.write(f"\033[{n}F")             # zurück zur Startposition
        sys.stdout.flush()

    def _render_confirm(self, name: str, preview: str) -> int:
        """Rendert den Bestätigungs-Block. Gibt die Anzahl ausgegebener Zeilen zurück."""
        use_frame = bool(self.config.get("tool_confirm_frame", True))
        lines_printed = 0

        if use_frame and self._confirm_console is not None:
            body = Text()
            body.append(f"Tool:  ", style="bold")
            body.append(f"{name}\n")
            if preview:
                body.append(f"Args:  ", style="bold")
                body.append(preview)
            panel = Panel(
                body,
                title="🔐 Tool-Bestätigung",
                border_style="bold red",
                padding=(0, 1),
            )
            # Zeilen zählen über Dummy-Console
            from io import StringIO
            buf = StringIO()
            dummy = Console(file=buf, width=self._confirm_console.width,
                            soft_wrap=True, highlight=False)
            dummy.print(panel)
            rendered = buf.getvalue()
            lines_printed += rendered.count("\n")
            self._confirm_console.print(panel)
            # Prompt unter dem Panel
            prompt_line = "   [y] allow  [n] deny  [r] deny+reason  [a] always"
            print(prompt_line)
            lines_printed += 1
        else:
            print(f"\n🔐 Bestätigung erforderlich: '{name}'")
            if preview:
                print(f"   {preview}")
            print("   [y] allow   [n] deny   [r] deny with reason   [a] always")
            lines_printed = 3 + (1 if preview else 0)

        sys.stdout.flush()
        return lines_printed

    def _normalize_verdict(self, res, name) -> Dict[str, Any]:
        if isinstance(res, dict):
            allow = bool(res.get("allow"))
            if allow and res.get("always"):
                self._session_approved.add(name)
            return {"allow": allow, "reason": str(res.get("reason", ""))}
        if isinstance(res, str):
            s = res.lower()
            if s == "always":
                self._session_approved.add(name)
                return {"allow": True, "reason": ""}
            return {"allow": s in ("yes", "y", "true"), "reason": ""}
        return {"allow": bool(res), "reason": ""}


    def _stdin_confirm(self, name: str, preview: str) -> Dict[str, Any]:
        try:
            lines = self._render_confirm(name, preview)
            ans = input("   Wahl [y/n/r/a]: ").strip().lower()

            reason_text = ""
            if ans in ("r", "reason"):
                reason_text = input("   Grund für die Ablehnung: ").strip()
                lines += 1

            if ans in ("a", "always"):
                self._session_approved.add(name)
                verdict = {"allow": True, "reason": ""}
            elif ans in ("y", "yes", "j", "ja"):
                verdict = {"allow": True, "reason": ""}
            elif ans in ("r", "reason"):
                verdict = {"allow": False, "reason": reason_text or "user denied"}
            else:
                verdict = {"allow": False, "reason": "user denied"}

            self._clear_lines(lines + 1)

            if verdict["allow"]:
                tag = "✓ erlaubt" + (" (session)" if ans in ("a", "always") else "")
            else:
                tag = f"✗ abgelehnt: {verdict['reason'][:60]}"
            print(f"🔐 {name} → {tag}")
            return verdict
        except (EOFError, KeyboardInterrupt):
            return {"allow": False, "reason": "user aborted"}


    def _ask_confirmation(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        preview = self._build_args_preview(arguments)
        # 1) Interface-Handler (CLI/GUI)
        if self.confirm_fn is not None:
            try:
                return self._normalize_verdict(self.confirm_fn(name, preview), name)
            except Exception as e:
                print(f"[ToolConfirm] Handler-Fehler: {e}")
        # 2) stdin nur im echten CLI-Kontext
        agent = getattr(self, "agent_ref", None)
        interface = getattr(agent, "interface", "") if agent else ""
        if interface == "cli" and sys.stdin.isatty():
            return self._stdin_confirm(name, preview)
        # 3) Config-Fallback (Daemon, Subagent, Bot)
        fallback = str(self.config.get("tool_confirm_fallback", "allow")).lower()
        return {"allow": fallback in ("allow", "yes"), "reason": ""}

    def validate_registry(self) -> Dict[str, List[str]]:
        pooled = {t.get("function", {}).get("name", "")
                  for t in self.available_tools
                  if not t.get("function", {}).get("name", "")
                         .startswith("mcp__")}
        handlers = set(self._HANDLERS)
        return {
            "pool_ohne_handler": sorted(pooled - handlers),
            "handler_ohne_pool": sorted(handlers - pooled),
        }

    # ---------- Argument-Previews (CLI-Tool-Log) ----------
    @staticmethod
    def _arg_is_path_like(key: str, value) -> bool:
        if not isinstance(value, str):
            return False
        k = str(key).lower()
        if any(t in k for t in ("path", "file", "dir", "folder",
                                "target", "dest", "source", "src")):
            return True
        return ("/" in value) or ("\\" in value)

    @staticmethod
    def _truncate_preview(value, budget: int, path_like: bool) -> str:
        s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        budget = max(budget, 8)
        marker = "(...)"
        if len(s) <= budget:
            return s
        keep = budget - len(marker)
        if path_like:
            return marker + s[-keep:]
        return s[:keep] + marker

    def format_args_preview(self, args) -> str:
        """Formt Tool-Argumente für die CLI-Log-Zeile."""
        total = int(self.config.get("tool_log_args_chars",
                      self.config.get("tool_log_args_length", 120)) or 120)
        if not isinstance(args, dict):
            s = json.dumps(args, ensure_ascii=False, default=str)
            return s[:total] + "(...)" if len(s) > total else s
        if not args:
            return ""
        per_min = int(self.config.get("tool_log_args_min_chars",
                        self.config.get("tool_log_args_per_min", 18)) or 18)
        items = list(args.items())
        per = max(per_min, total // len(items))
        parts = []
        for key, value in items:
            path_like = self._arg_is_path_like(key, value)
            parts.append(f"{key}: '{self._truncate_preview(value, per, path_like)}'")
        return " ".join(parts)

    def get_active_tools_info(self) -> List[Dict[str, Any]]:
        """Tool-Liste für /listtools etc. (public interface)."""
        tools = []
        for tool in self.available_tools:
            func = tool.get("function", {})
            tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "active": func.get("name", "") in self.active_tools})
        return tools

from . import tools as _tool_modules
_tool_modules.bind_tools(ToolManager)
