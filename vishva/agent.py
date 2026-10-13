import os
import io
import re
import sys
import copy
import time
import json
import string
import shutil
import base64
import random
import asyncio
import requests
import datetime
import threading
import subprocess
from rich.panel import Panel
from rich.markup import escape
from .paths import p, cfg_path
from rich.console import Console
from rich.markdown import Markdown
from .tool_manager import ToolManager
from prompt_toolkit.styles import Style
from prompt_toolkit import PromptSession
from .session_manager import SessionManager
from prompt_toolkit.formatted_text import HTML
from .tts_manager import TTSManager as TTSManager
from .code_manager import CodeManager, CODE_BLOCK_RE
from typing import Dict, List, Any, Optional
from .scheduler import TaskScheduler, process_due_tasks
from prompt_toolkit.completion import Completer, Completion

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

console = Console()

class AgentCore:
    def __init__(self, session_id=None, config_path: str = None, notifier=None, enable_tts: bool = False, telegram_mode: bool = False):
        self.config_path = config_path or p("config", "config.json")
        self.config = self._load_config()
        self.session_manager = SessionManager(self.config_path)
        self.session_manager.meta_call_fn = self._call_meta_api
        self.notifier = notifier
        self.notifications: List[str] = []
        self.base_url = self.config.get("base_url", "http://localhost:8080/v1")
        self.model = self.config.get("model", "llama3.1")
        self.api_key = self.config.get("api_key", "llama")
        self.tts_manager = TTSManager(self.config)
        if enable_tts:
            self.tts_manager.enabled = True
        self.telegram_mode = telegram_mode
        excluded = set()
        if not telegram_mode:
            excluded.add("send_image")
            excluded.add("send_file")
        self.tool_manager = ToolManager(
            tts_manager=self.tts_manager,
            config=self.config,
            exclude_tools=excluded)
        self.tool_manager.agent_ref = self
        self.tool_manager.set_confirm_handler(self._confirm_tool_cli)
        self._code_save_handler = None
        self.code_manager = CodeManager(
            self.config,
            debug_level_fn=self._debug_level,
            log_fn=lambda msg: console.print(msg))
        self.code_manager.set_handler(self._cli_code_save_handler)
        self.rag_manager = None
        if self.config.get("rag_enabled", False):
            try:
                from .rag_manager import RAGManager
                self.rag_manager = RAGManager(self.config)
                self.tool_manager.rag_manager = self.rag_manager
            except Exception as e:
                console.print(f"[yellow]⚠️ RAG deactivated: {type(e).__name__}: {e}[/yellow]")
        if self.rag_manager and bool(self.config.get("rag_preload_model", True)):
            try:
                delay = float(self.config.get("rag_preload_delay_seconds", 0) or 0)
                def _rag_warmup():
                    try:
                        if delay > 0:
                            time.sleep(delay)
                        result = self.rag_manager.preload_embedding_model()
                    except Exception:
                        pass
                threading.Thread(target=_rag_warmup, daemon=True).start()
            except Exception:
                pass
        self.offloading_enabled = self.config.get("offloading_enabled", True)
        self.sound_enabled = self.config.get("sound_enabled", True)
        self.show_thinking = self.config.get("show_thinking", False)
        self.context_size = self._load_context_size()
        raw_threshold = self.config.get("compression_threshold")
        self.compression_threshold = (
            self._calculate_compression_threshold(self.context_size)
            if raw_threshold is None else raw_threshold)
        self.soul = self._load_md("SOUL.md")
        self.engine_raw = self._load_md("ENGINE.md")
        self.system_prompt = self._build_system_prompt()
        self._chat_lock = threading.Lock()
        self._last_api_messages = None
        self._last_api_tools = None
        self._last_api_turn = 0
        if session_id:
            self.session_id = session_id
            if not self.session_manager.load_session(session_id):
                self.session_id = self.session_manager.create_session(self.system_prompt)
            else:
                history = self.session_manager.load_session(session_id)
                if history:
                    history[0]["content"] = self.system_prompt
                    self.session_manager.save_session(session_id, history)
                self._add_continuation_marker(session_id)
        else:
            latest = self.session_manager.get_latest_session()
            if latest:
                self.session_id = latest
                history = self.session_manager.load_session(latest)
                if history:
                    history[0]["content"] = self.system_prompt
                    self.session_manager.save_session(latest, history)
                self._add_continuation_marker(latest)
                console.print(f"[dim]📂 session continued: {latest}[/dim]")
            else:
                self.session_id = self.session_manager.create_session(self.system_prompt)
        self.tool_manager.agent_ref = self
        try:
            self.startup_tokens = self.get_total_tokens()
        except Exception:
            self.startup_tokens = -1
        self.last_session_tokens = self.startup_tokens
        self.interface = "telegram" if telegram_mode else "cli"
        self._chunk_callback = None

    def set_chunk_callback(self, callback):
        self._chunk_callback = callback

    @staticmethod
    def _split_answer_segments(content: str):
        segments = []
        last_end = 0
        for m in CODE_BLOCK_RE.finditer(content):
            if m.start() > last_end:
                segments.append(("md", content[last_end:m.start()]))
            segments.append(("code", m.group(1), m.group(2)))
            last_end = m.end()
        if last_end < len(content):
            tail = content[last_end:]
            m2 = re.search(r"^[ \t]*```([\w+-]*)[ \t]*\r?\n", tail, re.MULTILINE)
            if m2:
                if m2.start() > 0:
                    segments.append(("md", tail[:m2.start()]))
                segments.append(("code", m2.group(1), tail[m2.end():]))
            else:
                segments.append(("md", tail))
        return segments

    def _print_answer(self, content: str):
        frame_enabled = bool(self.config.get("cli_frame_enabled", False))
        frame_style = str(self.config.get("cli_frame_style", "cyan"))
        for seg in self._split_answer_segments(content):
            if seg[0] == "md":
                text = seg[1].strip()
                if not text:
                    continue
                if frame_enabled:
                    console.print(Panel(Markdown(text), border_style=frame_style, padding=(0, 1)))
                else:
                    console.print(Markdown(text))
            else:
                code = seg[2].rstrip("\n")
                if not code:
                    continue
                console.print(code, markup=False, highlight=False, emoji=False, soft_wrap=True)

    def _meta_endpoint(self, task: str = "meta"):
        key = {
            "summary": "summary_model",
            "rescue": "rescue_model",
            "enrichment": "rag_enrichment_model",
            "extraction": "rag_extraction_model",
        }.get(task, "")
        route = str(self.config.get(key, "meta")).lower() if key else "meta"
        if route == "main":
            return (
                str(self.config.get("base_url", "")).rstrip("/"),
                self.config.get("model", ""),
                self.config.get("api_key", "llama"),
                min(int(self.config.get("api_timeout", 300) or 300),
                    int(self.config.get("meta_main_timeout", 120))),)
        return (
            str(self.config.get("meta_model_url", "")).rstrip("/"),
            self.config.get("meta_model_name", ""),
            self.config.get("meta_api_key", self.config.get("api_key", "llama")),
            int(self.config.get("meta_model_timeout", 45) or 45),)

    def _cli_code_save_handler(self, block: Dict, idx: int, total: int) -> Optional[Dict]:
        try:
            if not sys.stdin.isatty():
                return None
        except Exception:
            return None
        lines = block["code"].count("\n") + 1
        lang = block["lang"] or "text"
        default = self._default_code_filename([block])
        self._pause_spinner()
        try:
            console.print(f"\n💾 Code block {idx}/{total} ({lang}, ~{lines} lines)")
            console.print(f"   Default filename: {default}")
            try:
                ans = input("   (Y)es / (n)o / (c)ustom / (s)kip all: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return {"save": False, "skip_all": True}
            if ans in ("n", "no"):
                return {"save": False}
            if ans in ("s", "skip"):
                return {"save": False, "skip_all": True}
            if ans in ("c", "custom"):
                try:
                    fn = input(f"   Filename [{default}]: ").strip()
                except (EOFError, KeyboardInterrupt):
                    return {"save": False}
                return {"save": True, "filename": fn or default}
            return {"save": True, "filename": default}
        finally:
            self._resume_spinner()

    def set_code_save_handler(self, fn):
        self.code_manager.set_handler(fn)

    def _default_code_filename(self, blocks: List[Dict]) -> str:
        return self.code_manager.default_code_filename(blocks)

    def _maybe_save_code(self, response: str) -> List[Dict[str, Any]]:
        return self.code_manager.maybe_save(response)

    def _build_code_fix_prompt(self, failed: List[Dict[str, Any]]) -> str:
        return self.code_manager.build_fix_prompt(failed)

    def _condense_trace_meta(self, trace: str, task: str = "rescue"):
        return SessionManager.condense_trace_meta(trace)

    @staticmethod
    def _repair_args_string(s) -> str:
        return SessionManager.repair_args_string(s)

    def _sanitize_history_tool_calls(self, history) -> bool:
        return self.session_manager.sanitize_history_tool_calls(history)

    @staticmethod
    def _strip_tool_history(messages):
        out = []
        for msg in messages:
            if msg.get("role") == "tool":
                continue
            m = dict(msg)
            if m.get("tool_calls"):
                m.pop("tool_calls", None)
                content = m.get("content") or ""
                m["content"] = (content + "\n[tool calls removed]").strip() if content else "[tool calls removed]"
            out.append(m)
        return out

    def _seed_bounds(self):
        try:
            bits = int(self.config.get("seed_bits", 64))
        except (TypeError, ValueError):
            bits = 64
        if bits not in (32, 64):
            bits = 64
        lo = -(2 ** (bits - 1))
        hi = 2 ** (bits - 1) - 1
        return lo, hi

    def _new_seed(self) -> int:
        lo, hi = self._seed_bounds()
        return random.randint(lo, hi)

    def _read_or_create_seed_file(self, path: str) -> int:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return int(f.read().strip())
        except Exception:
            pass
        seed = self._new_seed()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(seed))
        except Exception:
            pass
        return seed

    def _get_seed(self) -> int:
        mode = str(self.config.get("seed_mode", "random")).lower()
        path = self._seed_path(mode)
        if path is None:
            return self._new_seed()
        return self._read_or_create_seed_file(path)

    def _seed_dir(self) -> str:
        seed_dir = p("data", "seeds")
        try:
            os.makedirs(seed_dir, exist_ok=True)
        except Exception:
            pass
        return seed_dir

    def _seed_path(self, mode: str):
        if mode == "fixed":
            return os.path.join(self._seed_dir(), "fixed.seed")
        if mode == "session":
            sid = self.get_session_id() or "default"
            return os.path.join(self._seed_dir(), f"{sid}.seed")
        return None

    def _bg_rag_extract(self, user_message, assistant_answer, prev_user_msg, prev_asst_msg):
        try:
            ex_cfg = self.config
            if str(self.config.get("rag_extraction_model", "meta")).lower() == "main":
                ex_cfg = dict(self.config)
                ex_cfg["meta_model_url"] = self.config.get("base_url")
                ex_cfg["meta_model_name"] = self.config.get("model")
                ex_cfg["meta_api_key"] = self.config.get("api_key")
                ex_cfg["meta_model_timeout"] = min(
                    int(self.config.get("api_timeout", 300) or 300), 120)
            saved = self.rag_manager.extract_from_turn(
                prev_user_msg=prev_user_msg,
                prev_asst_msg=prev_asst_msg,
                user_message=user_message,
                assistant_answer=assistant_answer,
                config=ex_cfg)
            if saved and self.notifier:
                self.notifier(f"🧠 Extraction{'e' if saved != 1 else ''})")
        except Exception as e:
            if self.config.get("debug", 0) >= 1:
                console.print(f"[dim red]RAG-PostExtract-Fehler: {e}[/dim red]")

    def _enrichment_progress(self, text: str):
        """Leitet Enrichment-Zwischenantworten an den Streaming-Callback weiter."""
        if getattr(self, "_chunk_callback", None):
            try:
                self._chunk_callback({"type": "enrichment", "content": text})
            except Exception:
                pass

    def _print_assistant(self, text: str, title: str = "Assistant") -> None:
        if self.config.get("cli_frame_enabled", True):
            console.print(Panel(
                Markdown(text),
                title=f"[bold green]{title}[/bold green]",
                border_style="green"))
        else:
            console.print(Markdown(text))

    def _add_continuation_marker(self, session_id: str):
        self.session_manager.add_continuation_marker(session_id)
    _KEY_MIGRATION = {
        "max_read_file_size": "max_read_file_chars",
        "max_tool_args_length": "max_tool_args_chars",
        "max_tool_result_length": "max_tool_result_chars",
        "read_cache_chunk_size": "read_cache_chunk_chars",
        "tool_log_args_length": "tool_log_args_chars",
        "tool_log_args_per_min": "tool_log_args_min_chars",
        "rag_auto_extract_min_length": "rag_auto_extract_min_chars",
        "context_size": "context_tokens",
        "meta_context_size": "meta_context_tokens",}

    @classmethod
    def _migrate_config_keys(cls, config: Dict[str, Any]) -> bool:
        changed = False
        for old, new in cls._KEY_MIGRATION.items():
            if old in config and new not in config:
                config[new] = config.pop(old)
                changed = True
        return changed

    def _load_config(self) -> Dict[str, Any]:
        if os.path.exists(self.config_path):
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            if self._migrate_config_keys(config):
                try:
                    with open(self.config_path, "w", encoding="utf-8") as f:
                        json.dump(config, f, indent=2, ensure_ascii=False)
                except Exception:
                    pass
            return config
        return {}


    def _save_config(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)

    def _load_md(self, filename: str) -> str:
        path = filename if os.path.isabs(filename) else p("personas", "_active", filename)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def _load_context_size(self) -> int:
        try:
            ctx = int(self.config.get("context_tokens", self.config.get("context_size", 16384)) or 16384)
            return ctx if ctx >= 1024 else 16384
        except (TypeError, ValueError):
            return 16384

    def _calculate_compression_threshold(self, ctx_size: int) -> int:
        if ctx_size <= 8192:
            baseline = 3500
        else:
            baseline = 6000
        reserve = max(baseline, int(ctx_size * 0.25))
        return max(0, ctx_size - reserve)

    def _build_system_prompt(self) -> str:
        parts = [self.config.get("system_prompt", "")]
        if self.soul:
            parts.append(self.soul)
        if self.engine_raw:
            engine = self.engine_raw
            engine = engine.replace(
                "{agent_workdir}",
                self.config.get("agent_workdir", "working_dir"))
            if engine:
                parts.append(engine)
        return "\n\n".join(parts)

    def _get_system_metrics(self) -> str:
        try:
            import psutil
            mem = psutil.virtual_memory().percent
            cpu = psutil.cpu_percent(interval=0.1)
            metrics = f"mem:{mem}%|cpu:{cpu}%"
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    parts = result.stdout.strip().split(",")
                    if len(parts) >= 3:
                        gpu_util = int(float(parts[0]))
                        vram_used = int(float(parts[1]))
                        vram_total = int(float(parts[2]))
                        vram_pct = int((vram_used / vram_total) * 100)
                        metrics += f"|gpu:{gpu_util}%|vram:{vram_pct}%"
            except Exception:
                pass
            return metrics
        except Exception:
            return ""

    def _strip_thinking(self, content: str) -> str:
        if not content:
            return content
        original = content
        content = re.sub(r'<thinking>.*?</thinking>', '', content, flags=re.DOTALL)
        content = re.sub(r'<start_of_thinking>.*?</end_of_thinking>', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'<reasoning>.*?</reasoning>', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'^thought\s*[:\n]\s*', '', content, flags=re.IGNORECASE)
        content = re.sub(r'^reasoning\s*[:\n]\s*', '', content, flags=re.IGNORECASE)
        content = re.sub(r'^thinking\s*[:\n]\s*', '', content, flags=re.IGNORECASE)
        content = re.sub(r'```(?:thought|thinking|reasoning).*?```', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = content.strip()
        if not content and original.strip():
            return original.strip()
        return content

    def _refresh_system_prompt(self):
        self.system_prompt = self._build_system_prompt()
        history = self.session_manager.load_session(self.session_id)
        if history:
            history[0]["content"] = self.system_prompt
            self.session_manager.save_session(self.session_id, history)
    def _notify(self, message: str):
        self.notifications.append(message)
        if self.notifier:
            try:
                self.notifier(message)
            except Exception:
                pass

    def _debug_level(self) -> int:
        v = self.config.get("debug", 0)
        if isinstance(v, bool):
            return 2 if v else 0
        try:
            return max(0, min(2, int(v)))
        except (TypeError, ValueError):
            return 0

    def _process_image(self, path: str) -> Optional[str]:
        if not os.path.exists(path):
            return None
        try:
            from PIL import Image
            max_size = self.config.get("image_max_size", 256)
            img = Image.open(path)
            img.thumbnail((max_size, max_size), Image.LANCZOS)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            b64 = base64.b64encode(buf.getvalue()).decode()
            return f"data:image/jpeg;base64,{b64}"
        except Exception as e:
            console.print(f"[bold red]❌ image processing failed: {e}[/bold red]")
            return None

    def _sampling_params(self) -> Dict[str, Any]:
        return {
            "temperature": self.config.get("temperature", 0.9),
            "top_p": self.config.get("top_p", 0.95),
            "top_k": self.config.get("top_k", 64),
            "seed": self._get_seed(),}

    def _get_api_endpoints(self, wants_image: bool = False):
        base_url = self.base_url
        model = self.model
        api_key = self.api_key
        if not wants_image:
            return base_url, model, api_key
        if not bool(self.config.get("vision_enabled", True)):
            return base_url, model, api_key
        backend = str(self.config.get("vision_backend", "auto") or "auto").lower()
        if backend in ("none", "disabled"):
            return base_url, model, api_key
        vision_base_url = str(self.config.get("vision_base_url", "") or "").strip()
        vision_model = str(self.config.get("vision_model", "") or "").strip()
        vision_api_key = str(self.config.get("vision_api_key", "") or "").strip()
        if backend == "ollama" and not vision_base_url:
            vision_base_url = "http://127.0.0.1:11434/v1"
            if not vision_model:
                vision_model = "llava"
            if not vision_api_key:
                vision_api_key = "ollama"
        if vision_base_url:
            return (
                vision_base_url.rstrip("/"),
                vision_model or model,
                vision_api_key or api_key)
        return base_url, model, api_key

    def _call_api(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None) -> Dict[str, Any]:
        base_url = (base_url or self.base_url).rstrip("/")
        model = model or self.model
        api_key = api_key or self.api_key
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": messages,
            "stream": False}
        payload.update(self._sampling_params())
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        timeout = self.config.get("api_timeout", 1200)
        retry_delay = float(self.config.get("api_retry_delay", 1.5))
        max_attempts = int(self.config.get("api_max_retries", 1))
        last_error = None
        last_body = ""
        for attempt in range(max_attempts):
            try:
                if attempt > 0 and self._debug_level() >= 1:
                    console.print(f"[dim cyan]🔄 Retry attempt {attempt + 1}/{max_attempts}...[/dim cyan]")
                response = requests.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=timeout)
                if response.status_code >= 400:
                    body_msg = ""
                    try:
                        body_msg = str(response.json().get("error", {}).get("message", ""))
                    except Exception:
                        body_msg = (response.text or "")[:300]
                    err = requests.exceptions.HTTPError(
                        f"{response.status_code} Server Error: "
                        f"{body_msg[:200] or response.reason}",
                        response=response)
                    err.vishva_body = body_msg
                    raise err
                data = response.json()
                choice = data.get("choices", [{}])[0]
                msg = choice.get("message", {})
                reasoning = (msg.get("reasoning")
                             or msg.get("thinking")
                             or msg.get("reasoning_content")
                             or "")
                if reasoning:
                    data["_extracted_reasoning"] = reasoning
                return data
            except (requests.exceptions.RequestException,
                    requests.exceptions.HTTPError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                last_error = e
                last_body = getattr(e, "vishva_body", "") or ""
                if attempt < max_attempts - 1:
                    if self._debug_level() >= 1:
                        console.print(
                            f"[dim yellow]⚠️ API-Call failed "
                            f"(attempt {attempt + 1}/{max_attempts}), "
                            f"retry in {retry_delay}s: {e}[/dim yellow]")
                    time.sleep(retry_delay)
                    continue
                break
            except Exception as e:
                if self._debug_level() >= 1:
                    console.print(f"[red]❌ Unexpected error (no retry): {type(e).__name__}: {e}[/red]")
                return {"error": f"{type(e).__name__}: {str(e)}",
                        "error_body": "",
                        "error_kind": "other"}
        body = last_body or ""
        kind = ("tool_parse"
                if ("parse tool call arguments" in body
                    or "parse_error.101" in body)
                else "other")
        return {"error": str(last_error) if last_error else "Unknown error",
                "error_body": body[:300],
                "error_kind": kind}

    def _call_api_stream(self, messages: List[Dict[str, Any]],
                         tools: List[Dict[str, Any]],
                         chunk_callback,
                         base_url: Optional[str] = None,
                         model: Optional[str] = None,
                         api_key: Optional[str] = None) -> Dict[str, Any]:
        """Streaming-Variante von _call_api. Liefert Daten im gleichen Format,
        damit der Rest von _chat_impl unverändert weiterläuft."""
        base_url = (base_url or self.base_url).rstrip("/")
        model = model or self.model
        api_key = api_key or self.api_key
        headers = {"Authorization": f"Bearer {api_key}",
                   "Content-Type": "application/json"}
        payload = {"model": model, "messages": messages, "stream": True}
        payload.update(self._sampling_params())
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        timeout = self.config.get("api_timeout", 1200)

        full_content = ""
        full_reasoning = ""
        tool_calls_by_index = {}
        finish_reason = None

        try:
            response = requests.post(f"{base_url}/chat/completions",
                                     headers=headers, json=payload,
                                     timeout=timeout, stream=True)
            if response.status_code >= 400:
                try:
                    body_msg = str(response.json().get("error", {}).get("message", ""))
                except Exception:
                    body_msg = (response.text or "")[:300]
                return {"error": f"{response.status_code} Server Error: {body_msg[:200] or response.reason}",
                        "error_body": body_msg[:300], "error_kind": "other"}
            for raw_line in response.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="replace")
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choice = chunk.get("choices", [{}])[0]
                delta = choice.get("delta", {}) or {}
                finish_reason = choice.get("finish_reason") or finish_reason
                reasoning_piece = (delta.get("reasoning_content")
                                   or delta.get("reasoning")
                                   or delta.get("thinking") or "")
                if reasoning_piece:
                    full_reasoning += reasoning_piece
                    try:
                        chunk_callback({"type": "reasoning", "content": reasoning_piece})
                    except Exception:
                        pass
                content_piece = delta.get("content") or ""
                if content_piece:
                    full_content += content_piece
                    try:
                        chunk_callback({"type": "content", "content": content_piece})
                    except Exception:
                        pass
                for tc_delta in (delta.get("tool_calls") or []):
                    idx = tc_delta.get("index", 0)
                    slot = tool_calls_by_index.setdefault(
                        idx, {"id": "", "type": "function",
                              "function": {"name": "", "arguments": ""}})
                    if tc_delta.get("id"):
                        slot["id"] = tc_delta["id"]
                    if tc_delta.get("type"):
                        slot["type"] = tc_delta["type"]
                    fn = tc_delta.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
            tool_calls = [tool_calls_by_index[k] for k in sorted(tool_calls_by_index.keys())]
            message = {"role": "assistant", "content": full_content}
            if tool_calls:
                message["tool_calls"] = tool_calls
            data = {"choices": [{"message": message, "finish_reason": finish_reason}]}
            if full_reasoning:
                data["_extracted_reasoning"] = full_reasoning
            return data
        except (requests.exceptions.RequestException,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            return {"error": str(e), "error_body": "", "error_kind": "other"}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {str(e)}",
                    "error_body": "", "error_kind": "other"}

    def chat(self, user_message: str, image_b64: Optional[str] = None) -> str:
        with self._chat_lock:
            response = self._chat_impl(user_message, image_b64)
        # NEU: Live-Display beenden, damit Code-Save input() nutzen kann
        if getattr(self, "_chunk_callback", None):
            try:
                self._chunk_callback({"type": "done"})
                time.sleep(0.5)   # kurz warten, bis Live-Display wirklich zu ist
            except Exception:
                pass
        if not getattr(self, "_code_fix_active", False):
            saved = self._maybe_save_code(response)
            if saved:
                lines = []
                for s in saved:
                    line = f"💾 Code saved to: `{s['path']}`"
                    v = s.get("validation")
                    if v and v.get("checked"):
                        if v["ok"]:
                            line += " ✅ syntax OK"
                        else:
                            line += f" ⚠️ syntax error: {v['error']}"
                    lines.append(line)
                response += "\n\n" + "\n".join(lines)
                failed = [s for s in saved
                          if s.get("validation")
                          and s["validation"].get("checked")
                          and not s["validation"].get("ok")]
                if failed and bool(self.config.get("code_to_file_autofix", False)):
                    self._code_fix_active = True
                    try:
                        fix_response = self.chat(self._build_code_fix_prompt(failed))
                        response += "\n\n🔧 [Auto-Fix] " + str(fix_response)[:2000]
                    except Exception as e:
                        response += f"\n\n⚠️ Auto-Fix failed: {e}"
                    finally:
                        self._code_fix_active = False
        return response

    @staticmethod
    def _arg_is_path_like(key: str, value) -> bool:
        return ToolManager._arg_is_path_like(key, value)

    @staticmethod
    def _truncate_preview(value, budget: int, path_like: bool) -> str:
        return ToolManager._truncate_preview(value, budget, path_like)

    def _format_args_preview(self, args) -> str:
        return self.tool_manager.format_args_preview(args)

    def get_active_tools_info(self) -> List[Dict[str, Any]]:
        return self.tool_manager.get_active_tools_info()

    def _strip_old_code_blocks(self, history: List[Dict[str, Any]]):
        return self.session_manager.strip_old_code_blocks(history)

    def _strip_old_tool_comments(self, history: List[Dict[str, Any]]):
        return self.session_manager.strip_old_tool_comments(history)

    def _chat_impl(self, user_message: str, image_b64: Optional[str] = None) -> str:
        history = self.session_manager.load_session(self.session_id)
        if self._sanitize_history_tool_calls(history):
            self.session_manager.save_session(self.session_id, history)
        history, c1 = self._strip_old_code_blocks(history)
        history, c2 = self._strip_old_tool_comments(history)
        if c1 or c2:
            self.session_manager.save_session(self.session_id, history)
        if history and history[0].get("role") == "system":
            history[0]["content"] = self.system_prompt
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        dyn_parts = [f"[Current Date/Time: {now}]"]
        try:
            metrics = self._get_system_metrics()
            if metrics:
                dyn_parts.append(metrics)
        except Exception:
            pass
        dyn_block = "\n".join(dyn_parts)
        rag_ctx = ""
        if self.rag_manager:
            try:
                essential_ctx = self.rag_manager.format_essential_context() or ""
                prev_user_msg = ""
                prev_asst_msg = ""
                for msg in reversed(history[:-1]):
                    role = msg.get("role", "")
                    raw = msg.get("content", "")
                    if isinstance(raw, list):
                        raw = " ".join(
                            item.get("text", "") for item in raw
                            if isinstance(item, dict) and item.get("type") == "text")
                    else:
                        raw = str(raw)
                    if role == "user" and not prev_user_msg:
                        prev_user_msg = raw
                    elif role == "assistant" and not prev_asst_msg:
                        prev_asst_msg = raw
                    if prev_user_msg and prev_asst_msg:
                        break
                enrich_cfg = self.config
                if str(self.config.get("rag_enrichment_model", "meta")).lower() == "main":
                    enrich_cfg = dict(self.config)
                    enrich_cfg["meta_model_url"] = self.config.get("base_url")
                    enrich_cfg["meta_model_name"] = self.config.get("model")
                    enrich_cfg["meta_api_key"] = self.config.get("api_key")
                    enrich_cfg["meta_model_timeout"] = min(
                        int(self.config.get("api_timeout", 300) or 300), 120)
                enrichment_ctx = self.rag_manager.enrich_context(
                    user_message, enrich_cfg,
                    prev_user_msg=prev_user_msg,
                    prev_asst_msg=prev_asst_msg,
                    progress_callback=self._enrichment_progress) or ""
                if enrichment_ctx:
                    if self.notifier:
                        self.notifier("💉 Enrichment")
                    if essential_ctx:
                        rag_ctx = f"{essential_ctx}\n[RAG_ENRICHMENT]\n{enrichment_ctx}"
                    else:
                        rag_ctx = f"[RAG_ENRICHMENT]\n{enrichment_ctx}"
                elif essential_ctx:
                    rag_ctx = essential_ctx
            except Exception as e:
                if self.config.get("debug", 0) >= 1:
                    console.print(f"[dim red]RAG-Enrichment-Failure: {e}[/dim red]")
        ephemeral_parts = []
        if dyn_block:
            ephemeral_parts.append(dyn_block)
        if rag_ctx:
            ephemeral_parts.append(rag_ctx)
        ephemeral_suffix = "\n".join(ephemeral_parts)
        user_msg = {"role": "user", "content": user_message}
        if image_b64:
            user_msg["content"] = [
                {"type": "text", "text": user_message},
                {"type": "image_url", "image_url": {"url": image_b64}}]
        history.append(user_msg)
        self._collapse_history_images(history, keep_last_user=True)
        self.session_manager.save_session(self.session_id, history)
        if image_b64 and not bool(self.config.get("vision_enabled", True)):
            return (
                "⚠️ Vision ist deaktiviert. "
                "Setze `vision_enabled` in config.json auf true.")
        vision_active = bool(image_b64)
        api_base, api_model, api_key = self._get_api_endpoints(vision_active)
        tools = self.tool_manager.get_active_tools()
        if vision_active:
            tools = []
        api_messages = list(history)
        api_messages = self._inject_ephemeral_context(api_messages, ephemeral_suffix)
        session_tokens = self.session_manager.get_total_tokens(
            self.session_id, self.system_prompt, tools, override_history=api_messages)
        if session_tokens > self.compression_threshold:
            console.print(f"[dim yellow]⚠️  Session-Tokens ({session_tokens}) > Threshold ({self.compression_threshold}). Auto-Compression...[/dim yellow]")
            result = self.compress_history()
            console.print(f"[dim]{result}[/dim]")
            history = self.session_manager.load_session(self.session_id)
        max_tool_turns = self.config.get("max_tool_turns", 15)
        tool_turns = 0
        rescue_attempts = 0
        tool_parse_retries = 0
        while True:
            if tool_turns >= max_tool_turns:
                warning = (
                    f"⚠️ Termination after {tool_turns} Tool-Turn{'n' if tool_turns != 1 else ''} "
                    f"(Limit: {max_tool_turns}) – the model wanted additional tools "
                    "Execute (possible loop). Refine the query or start /new.")
                history.append({"role": "assistant", "content": warning})
                self.session_manager.save_session(self.session_id, history)
                return warning
            api_messages = list(history)
            api_messages = self._inject_ephemeral_context(api_messages, ephemeral_suffix)
            tools = self.tool_manager.get_active_tools()
            if vision_active:
                tools = []
            tokens_now = 0
            if tool_turns >= 1 or self.config.get("budget_hint_enabled", True) \
                    or self.config.get("loop_rescue_enabled", True):
                try:
                    tokens_now = self.session_manager.get_total_tokens(
                        self.session_id, self.system_prompt, tools, override_history=api_messages)
                except Exception:
                    tokens_now = 0
            reserve = int(self.config.get("loop_rescue_reserve", 4000))
            if tokens_now and tokens_now > self.context_size - reserve:
                if (self.config.get("loop_rescue_enabled", True)
                        and rescue_attempts < int(self.config.get("loop_rescue_max_attempts", 2))):
                    rescue_attempts += 1
                    history, rescue_log = self._rescue_context(history, rescue_attempts)
                    console.print(f"[dim yellow]🆘 {rescue_log}[/dim yellow]")
                    continue
                else:
                    trace = self._extract_tool_trace(history)
                    warn = ("⚠️ Context almost full – task aborted (tool results too large)."
                            "Please divide the task into smaller portions.\n[PROGRESS]\n" + trace)
                    history.append({"role": "assistant", "content": warn})
                    self.session_manager.save_session(self.session_id, history)
                    return warn
            tool_turns += 1
            api_messages = self._inject_budget(api_messages, tool_turns, max_tool_turns, tokens_now)
            api_messages = self._inject_tool_budget(api_messages, tool_turns, max_tool_turns)
            self._last_api_messages = copy.deepcopy(api_messages)
            self._last_api_tools = tools
            self._last_api_turn = tool_turns
            start_time = time.time()
            if bool(self.config.get("enable_stream", False)) and self._chunk_callback:
                data = self._call_api_stream(
                    api_messages,
                    tools,
                    chunk_callback=self._chunk_callback,
                    base_url=api_base,
                    model=api_model,
                    api_key=api_key)
            else:
                data = self._call_api(
                    api_messages,
                    tools,
                    base_url=api_base,
                    model=api_model,
                    api_key=api_key)
            elapsed = time.time() - start_time
            completion_tokens = data.get("usage", {}).get("completion_tokens", 0)
            if completion_tokens and elapsed > 0:
                self.last_tps = completion_tokens / elapsed
            else:
                content = (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
                est_tokens = max(len(content) // 4, 1)
                self.last_tps = est_tokens / elapsed if elapsed > 0 else 0.0
            if "error" in data:
                if data.get("error_kind") == "tool_parse" and tools:
                    if tool_parse_retries < 1:
                        tool_parse_retries += 1
                        console.print("[dim yellow]🛠️ Tool Call JSON broken → Retry with repair note[/dim yellow]")
                        hint = ("[SYSTEM NOTE] Your previous tool call contained invalid JSON "
                                "(literal newlines inside a string value). Keep tool arguments "
                                "as single-line JSON and escape newlines as \\n. Retry the call now.")
                        retry_msgs = list(api_messages) + [{"role": "user", "content": hint}]
                        data = self._call_api(retry_msgs, tools,
                                            base_url=api_base, model=api_model,
                                            api_key=api_key)
                    if "error" in data and data.get("error_kind") == "tool_parse":
                        console.print("[dim yellow]🛠️ Still broken → Answer without tools (History ent-tool-t)[/dim yellow]")
                        stripped = self._strip_tool_history(api_messages)
                        stripped.append({
                            "role": "user",
                            "content": ("[SYSTEM NOTE] Tools are DISABLED for this turn. "
                                        "Do NOT emit tool-call JSON or <tool_call> blocks. "
                                        "Answer in plain markdown; put code in ``` blocks.")})
                        data = self._call_api(stripped, [],
                                            base_url=api_base, model=api_model,
                                            api_key=api_key)
                if "error" in data:
                    error_msg = f"API-ERROR: {data['error']}"
                    history.append({"role": "assistant", "content": error_msg})
                    if data.get("error_kind") == "tool_parse":
                        history.append({"role": "user", "content": (
                            "[SYSTEM NOTE] Your tool call "
                            "failed due to invalid JSON (literal newlines in the arguments). From now on, output code "
                            "as a ```codeblock``` in the response instead of calling `write_file` — "
                            "or escape newlines as `\\n`.")})
                    self.session_manager.save_session(self.session_id, history)
                    return error_msg
            choice = data.get("choices", [{}])[0]
            assistant_msg = choice.get("message", {})
            content = assistant_msg.get("content", "") or ""
            tool_calls = assistant_msg.get("tool_calls", [])
            reasoning = data.get("_extracted_reasoning", "")
            if not content.strip() and reasoning.strip() and not tool_calls:
                content = reasoning
                reasoning = ""
            if not content.strip() and not tool_calls:
                thinking_raw = (
                    reasoning
                    or assistant_msg.get("reasoning", "")
                    or assistant_msg.get("reasoning_content", "")
                    or assistant_msg.get("thinking", "")
                    or ""
                ).strip()
                if not thinking_raw and content.strip():
                    if not self._strip_thinking(content).strip():
                        thinking_raw = content.strip()
                if thinking_raw:
                    console.print(Panel(
                        thinking_raw[:2000],
                        title="[bold yellow]💭 Thinking (Antwort war leer)[/bold yellow]",
                        border_style="yellow"))
                    self._notify(f"💭 Thinking (leere Antwort): {thinking_raw[:100]}…")
                error_msg = ("⚠️ The model returned an empty response. "
                             "Please repeat the question or start /new.")
                if thinking_raw and (self.show_thinking or self._debug_level() >= 1):
                    error_msg += f"\n[THINKING]\n{thinking_raw[:1500]}"
                history.append({"role": "assistant", "content": error_msg})
                self.session_manager.save_session(self.session_id, history)
                return error_msg
            assistant_entry = {"role": "assistant", "content": content}
            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function", {})
                    func["arguments"] = self._repair_args_string(func.get("arguments", "{}"))
                assistant_entry["tool_calls"] = tool_calls
            history.append(assistant_entry)
            self.session_manager.save_session(self.session_id, history)
            if not tool_calls:
                if reasoning and self.show_thinking:
                    content = f"[THINKING]\n{reasoning}\n\n[ANSWER]\n{content}"
                elif not self.show_thinking:
                    content = self._strip_thinking(content)
                if self._collapse_history_images(history):
                    self.session_manager.save_session(self.session_id, history)
                content = content.replace(r"$\rightarrow$", "→").replace(r"$\Rightarrow$", "→").replace(r"$\leftarrow$", "🠄").replace(r"$\Leftarrow$", "🠄").replace(r"$\leftrightarrow$", "↔").replace(r"$\Leftrightarrow$", "↔").replace(r"$\to$", "→").replace(r"$\To$", "→").replace(r"$\implies$", "⇒").replace(r"$\Implies$", "⇒").replace(r"$\iff$", "⇔").replace(r"$\Iff$", "⇔").replace(r"$\neg$", "¬").replace(r"$\Neg$", "¬").replace(r"$\in$", "∈").replace(r"$\In$", "∈").replace(r"$\notin$", "∉").replace(r"$\Notin$", "∉").replace(r"$\subset$", "⊂").replace(r"$\Subset$", "⊂").replace(r"$\subseteq$", "⊆").replace(r"$\Subseteq$", "⊆").replace(r"$\cap$", "∩").replace(r"$\Cap$", "∩").replace(r"$\cup$", "∪").replace(r"$\Cup$", "∪").replace(r"$\neq$", "≠︎").replace(r"$\Neq$", "≠︎").replace(r"$\approx$", "≈").replace(r"$\Approx$", "≈").replace(r"$\equiv$", "≅︎").replace(r"$\Equiv$", "≅︎").replace(r"$\pm$", "±").replace(r"$\Pm$", "±").replace(r"$\infty$", "∞").replace(r"$\Infty$", "∞").replace(r"$\forall$", "∀").replace(r"$\Forall$", "∀").replace(r"$\exists$", "∃").replace(r"$\Exists$", "∃").replace(r"$\sum$", "∑").replace(r"$\Sum$", "∑").replace(r"$\int$", "∫").replace(r"$\Int$", "∫")
                expanded_check = list(history)
                session_tokens = self.session_manager.get_total_tokens(
                    self.session_id, self.system_prompt, tools, override_history=expanded_check)
                if session_tokens > self.compression_threshold:
                    console.print(f"[dim yellow]⚠️  Session-Tokens ({session_tokens}) > Threshold ({self.compression_threshold}). Auto-Compression...[/dim yellow]")
                    result = self.compress_history()
                    console.print(f"[dim]{result}[/dim]")
                    history = self.session_manager.load_session(self.session_id)
                    expanded_check = list(history)
                    session_tokens = self.session_manager.get_total_tokens(self.session_id, self.system_prompt, tools, override_history=expanded_check)
                history = self.session_manager.compress_history_pointers(
                    self.session_id, history, self.offloading_enabled,
                    skip_last_tool_results=0)
                history = self.session_manager.apply_tool_ttl(history, self.session_id)
                self.session_manager.save_session(self.session_id, history)
                try:
                    self.last_session_tokens = self.get_total_tokens()
                except Exception:
                    self.last_session_tokens = session_tokens
                if not content or not content.strip():
                    content = "(The model did not provide a response. Tool calls were executed.)"
                if self.rag_manager and bool(self.config.get("rag_meta_extraction_enabled", True)):
                    try:
                        prev_user_msg = ""
                        prev_asst_msg = ""
                        for msg in reversed(history[:-2]):
                            role = msg.get("role", "")
                            raw = msg.get("content", "")
                            if isinstance(raw, list):
                                raw = " ".join(
                                    item.get("text", "") for item in raw
                                    if isinstance(item, dict) and item.get("type") == "text")
                            else:
                                raw = str(raw)
                            if role == "user" and not prev_user_msg:
                                prev_user_msg = raw
                            elif role == "assistant" and not prev_asst_msg:
                                prev_asst_msg = raw
                            if prev_user_msg and prev_asst_msg:
                                break
                        threading.Thread(
                            target=self._bg_rag_extract,
                            args=(user_message, content, prev_user_msg, prev_asst_msg),
                            daemon=True).start()
                    except Exception as e:
                        if self.config.get("debug", 0) >= 1:
                            console.print(f"[dim red]RAG-PostExtract-Fehler: {e}[/dim red]")
                return content
            if content and content.strip():
                stream_active = bool(self.config.get("enable_stream", False)) and self._chunk_callback
                show_intermediate = bool(self.config.get("cli_show_intermediate_content", True))
                if show_intermediate and not stream_active:
                    display = self._strip_thinking(content)
                    self._print_answer(display)
                    console.print()
            memory_tools_used = False
            round_max_res = self.config.get("max_tool_result_chars", 1000)
            if self.config.get("loop_adaptive_cap", True) and tokens_now and tokens_now > int(self.context_size * 0.6):
                round_max_res = max(1200, round_max_res // 2)
            for tc in tool_calls:
                if tc.get("type") == "function":
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    tc_id = tc.get("id", "unknown")
                    try:
                        args = json.loads(func.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        args = {}
                    emoji_map = {
                        "write_file": "📝",
                        "read_file": "📖",
                        "edit_file": "✏️",
                        "cancel_task": "❌",
                        "ls_tasks": "📋",
                        "sched_task": "⏰",
                        "speak": "🔊",
                        "web_search": "🌐",
                        "web_read": "🕸️",
                        "product_search": "🛒",
                        "spotify": "🎧",
                        "list_dir": "📁",
                        "read_cache": "🔍",
                        "sys_clean": "🧹",
                        "sys_update": "📦",
                        "bash": "⚡",
                        "bg_task": "🚀",
                        "cd": "🫚",
                        "weather": "🌤️",
                        "tarot": "🔮",
                        "news_digest": "📰",
                        "brightness_ctl": "💡",
                        "lint_code": "🔎",}
                    emoji = emoji_map.get(name, "🔧")
                    args_preview = self._format_args_preview(args)
                    log_msg = f"{emoji} {name} {args_preview}"
                    if bool(self.config.get("enable_stream", False)) and self._chunk_callback:
                        try:
                            self._chunk_callback({"type": "tool_log", "content": log_msg})
                        except Exception:
                            console.print(f"[bold yellow]{escape(log_msg)}[/bold yellow]")
                    else:
                        console.print(f"[bold yellow]{escape(log_msg)}[/bold yellow]")
                    self._notify(log_msg)
                    result = self.tool_manager.execute_tool(name, args)
                    result_str = json.dumps(result, ensure_ascii=False, default=str)
                    if self.offloading_enabled:
                        sm = self.session_manager
                        entry = sm._load_cache_entry(self.session_id, tc_id) or {}
                        entry.update({
                            "name": name,
                            "arguments": func.get("arguments", "{}"),
                            "result": result_str})
                        n_chunks = sm.save_chunked_cache(
                            self.session_id, tc_id, entry,
                            chunk_size=self.config.get("read_cache_chunk_chars", 1000))
                        dbg = self._debug_level()
                        if dbg == 1:
                            console.print("💾")
                        elif dbg == 2:
                            console.print(
                                f"[dim red]DEBUG 💾 {name}: {len(result_str)} chars "
                                f"→ {n_chunks} chunks cached[/dim red]")
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": name,
                        "content": result_str})
                    self.session_manager.save_session(self.session_id, history)
            min_turns = int(self.config.get("offloading_min_tool_turns", 0) or 0)
            if self.offloading_enabled and tool_turns > min_turns:
                history = self.session_manager.compress_history_pointers(
                    self.session_id, history, self.offloading_enabled,
                    skip_last_tool_results=len(tool_calls))
                self.session_manager.save_session(self.session_id, history)
                history = self.session_manager.load_session(self.session_id)
            send_image_markers = []
            for tc in tool_calls:
                if tc.get("type") == "function":
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    tc_id = tc.get("id", "unknown")
                    if name == "send_image":
                        for msg in history:
                            if msg.get("role") == "tool" and msg.get("tool_call_id") == tc_id:
                                try:
                                    result = json.loads(msg.get("content", "{}"))
                                    marker = result.get("marker", "")
                                    if marker and marker.startswith("[SEND_IMAGE:"):
                                        send_image_markers.append(marker)
                                except (json.JSONDecodeError, AttributeError):
                                    pass
                        break
            if send_image_markers:
                self._pending_image_markers = send_image_markers

    def _call_meta_api(self, messages: List[Dict[str, Any]], max_tokens: int = 512,
                       task: str = "meta"):
        meta_max = int(self.config.get("meta_model_max_tokens", 0) or 0)
        if meta_max > 0:
            max_tokens = meta_max
        base, model, api_key, timeout = self._meta_endpoint(task)
        data = self._post_chat(
            base=base,
            model=model,
            api_key=api_key,
            timeout=timeout,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.2,
            label=f"meta:{task}")
        if data:
            return data
        if bool(self.config.get("meta_fallback_enabled", True)):
            if base == str(self.config.get("base_url", "")).rstrip("/"):
                return None
            return self._post_chat(
                base=self.base_url,
                model=self.model,
                api_key=self.api_key,
                timeout=min(int(self.config.get("api_timeout", 300) or 300), 180),
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.2,
                label="main")
        return None

    def _post_chat(self, base, model, api_key, timeout, messages, max_tokens, temperature, label=""):
        url = str(base or "").strip()
        if not url or not model:
            return None
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": temperature,}
        if bool(self.config.get("meta_send_thinking_kwargs", False)):
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        try:
            r = requests.post(f"{url.rstrip('/')}/chat/completions", headers=headers, json=payload, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            msg = data.get("choices", [{}])[0].get("message", {})
            content = (msg.get("content", "") or "").strip()
            if not content:
                content = (msg.get("reasoning_content", "") or msg.get("reasoning", "") or msg.get("thinking", "") or "").strip()
                if content:
                    msg["content"] = content
                    data["_used_reasoning_fallback"] = True
            if not content:
                return None
            return data
        except Exception as e:
            if self._debug_level() >= 2:
                console.print(f"[dim red]Meta-API ({label or 'model'}) Fehler: {e}[/dim red]")
            return None

    def _extract_tool_trace(self, history: List[Dict[str, Any]]) -> str:
        return self.session_manager.extract_tool_trace(history)

    def _last_tool_pairs(self, history: List[Dict[str, Any]], keep: int) -> List[Dict[str, Any]]:
        return self.session_manager.last_tool_pairs(history, keep)

    @staticmethod
    def _tool_result_status(res_raw: str) -> str:
        return SessionManager._tool_result_status(res_raw)

    def _meta_char_budget(self, max_out_tokens: int, task: str = "meta") -> int:
        try:
            meta_ctx = int(self.config.get("meta_context_tokens", self.config.get("meta_context_size", 4096)) or 4096)
        except (TypeError, ValueError):
            meta_ctx = 4096
        instruction_tokens = 150
        safety = int(meta_ctx * 0.1)
        input_tokens = max(500, meta_ctx - max_out_tokens - instruction_tokens - safety)
        return int(input_tokens * 3)

    def _condense_chunk(self, chunk: str, max_out: int, part_label: str = "", task: str = "meta") -> Optional[str]:
        prompt = (
            "Condense this agent's tool trace into a concise "
            "progress report (max. 150 words). Include: goal, completed steps "
            "(files/commands/results), open issues, next step."
            + (f" This is part {part_label} of a longer trace." if part_label else "")
            + "\nTRACE:\n" + chunk)
        data = self._call_meta_api([{"role": "user", "content": prompt}], max_tokens=max_out, task=task)
        if data:
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content and content.strip():
                return content.strip()
        return None

    def _merge_partials(self, merged: str, max_out: int, task: str = "meta") -> Optional[str]:
        prompt = (
            "Merge these partial progress reports of ONE ongoing task into a "
            "single concise progress report (max. 150 words). Include: goal, "
            "completed steps, open issues, next step.\nPARTIALS:\n" + merged)
        data = self._call_meta_api([{"role": "user", "content": prompt}], max_tokens=max_out)
        if data:
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content and content.strip():
                return content.strip()
        return None

    def _rescue_context(self, history: List[Dict[str, Any]], attempt: int):
        return self.session_manager.rescue_context(
            self.session_id, history, attempt,
            condense_fn=self._condense_trace_meta,
            workdir=self.tool_manager.workdir)

    def _inject_budget(
        self,
        api_messages: List[Dict[str, Any]],
        tool_turns: int,
        max_tool_turns: int,
        tokens_now: int) -> List[Dict[str, Any]]:
        if not self.config.get("budget_hint_enabled", True) or not api_messages:
            return api_messages
        free = max(0, self.context_size - tokens_now)
        pct = int((tokens_now / self.context_size) * 100) if self.context_size else 0
        hint = (
            f"\n[BUDGET] Context {pct}% in use (~{free // 1000}k free). "
            f"If >80%: finish instead of starting over.")
        user_idx = None
        for i in range(len(api_messages) - 1, -1, -1):
            if api_messages[i].get("role") == "user":
                user_idx = i
                break
        if user_idx is None:
            return api_messages
        new_messages = list(api_messages)
        msg = dict(new_messages[user_idx])
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = content + hint
        elif isinstance(content, list):
            new_content = []
            appended = False
            for item in content:
                if isinstance(item, dict):
                    item_copy = dict(item)
                    if not appended and item_copy.get("type") == "text":
                        item_copy["text"] = str(item_copy.get("text", "")) + hint
                        appended = True
                    new_content.append(item_copy)
                else:
                    new_content.append(item)
            if not appended:
                new_content.append({"type": "text", "text": hint})
            msg["content"] = new_content
        else:
            msg["content"] = str(content or "") + hint
        new_messages[user_idx] = msg
        return new_messages

    def _inject_tool_budget(
        self,
        api_messages: List[Dict[str, Any]],
        tool_turns: int,
        max_tool_turns: int) -> List[Dict[str, Any]]:
        if not self.config.get("budget_hint_enabled", True) or not api_messages:
            return api_messages
        last_tool_idx = None
        for i in range(len(api_messages) - 1, -1, -1):
            if api_messages[i].get("role") == "tool":
                last_tool_idx = i
                break
        if last_tool_idx is None:
            return api_messages
        left = max(0, max_tool_turns - tool_turns)
        note = f"\n[TOOL_BUDGET: {left}/{max_tool_turns} tool turns remaining"
        if left <= 3:
            note += " — wrap up current task, do not start new tools"
        note += "]"
        new_messages = list(api_messages)
        msg = dict(new_messages[last_tool_idx])
        content = msg.get("content", "")
        if isinstance(content, str):
            msg["content"] = content + note
        else:
            msg["content"] = str(content or "") + note
        new_messages[last_tool_idx] = msg
        return new_messages

    def _inject_ephemeral_context(self, api_messages: List[Dict[str, Any]], suffix: str) -> List[Dict[str, Any]]:
        if not suffix or not api_messages:
            return api_messages
        user_idx = None
        for i in range(len(api_messages) - 1, -1, -1):
            if api_messages[i].get("role") == "user":
                user_idx = i
                break
        if user_idx is None:
            return api_messages
        msg = dict(api_messages[user_idx])
        content = msg.get("content")
        add = "\n\n" + suffix
        if isinstance(content, str):
            msg["content"] = content + add
        elif isinstance(content, list):
            new_content = []
            appended = False
            for item in content:
                if isinstance(item, dict):
                    item_copy = dict(item)
                    if not appended and item_copy.get("type") == "text":
                        item_copy["text"] = str(item_copy.get("text", "")) + add
                        appended = True
                    new_content.append(item_copy)
                else:
                    new_content.append(item)
            if not appended:
                new_content.insert(0, {"type": "text", "text": add.lstrip("\n")})
            msg["content"] = new_content
        else:
            msg["content"] = str(content or "") + add
        new_messages = list(api_messages)
        new_messages[user_idx] = msg
        return new_messages

    def _collapse_history_images(self, history: List[Dict[str, Any]], keep_last_user: bool = False) -> bool:
        return self.session_manager.collapse_history_images(history, keep_last_user)

    def get_session_id(self) -> str:
        return self.session_id

    def get_total_tokens(self) -> int:
        tools = self.tool_manager.get_active_tools()
        return self.session_manager.get_total_tokens(self.session_id, self.system_prompt, tools)

    def _refresh_session_tokens(self):
        try:
            self.last_session_tokens = self.get_total_tokens()
        except Exception:
            self.last_session_tokens = 0

    def start_new_session(self):
        self.tool_manager.reset_cwd()
        self.session_id = self.session_manager.create_session(self._build_system_prompt())
        self.notifications.clear()
        self.last_tps = None
        self._refresh_session_tokens()

    def switch_to(self, session_id: str):
        self.tool_manager.reset_cwd()
        if os.path.exists(p("data", "sessions", f"{session_id}.json")):
            self.session_id = session_id
            history = self.session_manager.load_session(session_id)
            if history:
                history[0]["content"] = self._build_system_prompt()
                self.session_manager.save_session(session_id, history)
            self._add_continuation_marker(session_id)
            self._refresh_session_tokens()
        else:
            raise ValueError(f"Session {session_id} not found.")

    def clear_current(self):
        self.session_manager.clear_session(self.session_id, self._build_system_prompt())
        self.notifications.clear()
        self._refresh_session_tokens()

    def compress_history(self) -> str:
        result = self.session_manager.compress_history(self.session_id, rag_offload=False)
        self._refresh_session_tokens()
        if result:
            old_len = result.get("old_len", "?")
            new_len = result.get("new_len", "?")
            return f"✅ Compressed: {old_len} → {new_len} Messages"
        return "⚠️ Nothing to compress."

    def _parse_persona_file(self, path: str) -> Dict[str, str]:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        sections = {}
        current_section = None
        current_lines = []
        for line in content.splitlines(keepends=True):
            stripped = line.strip()
            if stripped.startswith("---") and stripped.endswith("---") and len(stripped) > 6:
                if current_section:
                    sections[current_section] = "".join(current_lines).strip()
                current_section = stripped.strip("-").strip().lower()
                current_lines = []
            elif current_section:
                current_lines.append(line)
        if current_section and current_lines:
            sections[current_section] = "".join(current_lines).strip()
        return sections

    def set_personality(self, name: str) -> Dict[str, Any]:
        personas_dir = p("personas")
        if not os.path.exists(personas_dir):
            return {"success": False, "error": "personas/ directory not found."}
        persona_file = os.path.join(personas_dir, f"{name}.md")
        if not os.path.exists(persona_file):
            available = sorted([
                f.replace(".md", "")
                for f in os.listdir(personas_dir)
                if f.endswith(".md")])
            return {
                "success": False,
                "error": f"Personality '{name}' not found. Available: {', '.join(available)}"}
        try:
            sections = self._parse_persona_file(persona_file)
            soul_content = sections.get("soul", "")
            if soul_content:
                with open(p("personas", "_active", "SOUL.md"), "w", encoding="utf-8") as f:
                    f.write(soul_content)
            engine_content = sections.get("engine", "")
            if engine_content:
                with open(p("personas", "_active", "ENGINE.md"), "w", encoding="utf-8") as f:
                    f.write(engine_content)
            active_content = sections.get("activetools", "").strip()
            if active_content:
                with open(p("config", "activetools.txt"), "w", encoding="utf-8") as f:
                    f.write(active_content + "\n")
            self.soul = self._load_md("SOUL.md")
            self.engine_raw = self._load_md("ENGINE.md")
            self.tool_manager._load_or_init()
            self.tool_manager._save_activetools()
            self._refresh_system_prompt()
            return {"success": True, "message": f"Personality switched to '{name}'."}
        except Exception as e:
            return {"success": False, "error": str(e)}


    def _pause_spinner(self):
        st = getattr(self, "_active_status", None)
        if st:
            try: st.stop()
            except Exception: pass

    def _resume_spinner(self):
        st = getattr(self, "_active_status", None)
        if st:
            try: st.start()
            except Exception: pass

    def _confirm_tool_cli(self, tool_name: str, preview: str):
        self._pause_spinner()
        try:
            return self.tool_manager._stdin_confirm(tool_name, preview)
        finally:
            self._resume_spinner()

def main():
    from .cli import main as _cli_main
    return _cli_main()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[bold yellow]bye 👋🏻[/bold yellow]")
