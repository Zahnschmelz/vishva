import os
import json
import asyncio
import datetime
import re
import subprocess
import shutil
import base64
import io
import time
import requests
import threading
from .paths import p, cfg_path
from .tts_manager import TTSManager as TTSManager
from typing import Dict, List, Any, Optional, Callable
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import HTML
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from .tool_manager import ToolManager
from .session_manager import SessionManager
from .scheduler import TaskScheduler, process_due_tasks

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

CODE_BLOCK_RE = re.compile(r"```(\S*)[ \t]*\n(.*?)\n?```", re.DOTALL)
console = Console()
def _play_notification_sound():
    for fname in ("alarm.mp3", "alarm.wav"):
        fpath = p("assets", "sounds", fname)
        if os.path.exists(fpath):
            for player in ("paplay", "aplay", "ffplay"):
                if shutil.which(player):
                    cmd = [player, fpath] if player != "ffplay" else \
                          ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", fpath]
                    try:
                        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        return
                    except Exception:
                        pass
    return

class CommandCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text
        if not text.startswith('/'):
            return
        commands = [
            '/exit', '/bye', '/new', '/session', '/clear', '/history',
            '/tokens', '/config', '/voice', '/info', '/zip', '/shrink',
            '/listtools', '/offloading', '/sched',
            '/sound', '/showThinking', '/image', '/help', '/threshold',
            '/stt', '/personality', '/persona', '/frame']
        for cmd in commands:
            if cmd.startswith(text):
                yield Completion(cmd, start_position=-len(text))

class AgentCore:
    def __init__(self, session_id=None, config_path: str = None, notifier=None, enable_tts: bool = False, telegram_mode: bool = False):
        self.config_path = config_path or p("config", "config.json")
        self.config = self._load_config()
        self.session_manager = SessionManager(self.config_path)
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
        self.rag_manager = None
        if self.config.get("rag_enabled", False):
            try:
                from .rag_manager import RAGManager
                self.rag_manager = RAGManager(self.config)
                self.tool_manager.rag_manager = self.rag_manager
            except Exception as e:
                console.print(f"[yellow]⚠️ RAG deaktiviert: {type(e).__name__}: {e}[/yellow]")
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
            segments.append(("md", content[last_end:]))
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

    def _bg_rag_extract(self, user_message, assistant_answer, prev_user_msg, prev_asst_msg):
        try:
            saved = self.rag_manager.extract_from_turn(
                prev_user_msg=prev_user_msg,
                prev_asst_msg=prev_asst_msg,
                user_message=user_message,
                assistant_answer=assistant_answer,
                config=self.config)
            if saved and self.notifier:
                self.notifier(f"🧠 Extraction{'e' if saved != 1 else ''}")
        except Exception as e:
            if self.config.get("debug", 0) >= 1:
                console.print(f"[dim red]RAG-PostExtract-Fehler: {e}[/dim red]")

    def _print_assistant(self, text: str, title: str = "Assistant") -> None:
        if self.config.get("cli_frame_enabled", True):
            console.print(Panel(
                Markdown(text),
                title=f"[bold green]{title}[/bold green]",
                border_style="green"))
        else:
            console.print(Markdown(text))

    def _add_continuation_marker(self, session_id: str):
        history = self.session_manager.load_session(session_id)
        if not history or len(history) <= 1:
            return
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        marker_text = (
            f"[SYSTEM NOTE / SESSION_CONTINUED: {now}] "
            "Note: Time has passed since the last interaction. "
            "This is a continuation of a previous conversation.")
        last_msg = history[-1]
        last_content = str(last_msg.get("content", ""))
        if "[SESSION_CONTINUED:" in last_content:
            history[-1]["content"] = marker_text
            history[-1]["role"] = "user"
        else:
            history.append({
                "role": "user",
                "content": marker_text})
        self.session_manager.save_session(session_id, history)

    def _load_config(self) -> Dict[str, Any]:
        if os.path.exists(self.config_path):
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
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
            ctx = int(self.config.get("context_size", 16384) or 16384)
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
        params: Dict[str, Any] = {}
        if self.config.get("temperature") is not None:
            params["temperature"] = float(self.config["temperature"])
        if self.config.get("top_p") is not None:
            params["top_p"] = float(self.config["top_p"])
        if self.config.get("top_k") is not None:
            params["top_k"] = int(self.config["top_k"])
        if self.config.get("seed") is not None:
            params["seed"] = int(self.config["seed"])
        return params

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
        max_attempts = 2

        for attempt in range(max_attempts):
            try:
                response = requests.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=timeout)
                response.raise_for_status()
                data = response.json()
                choice = data.get("choices", [{}])[0]
                msg = choice.get("message", {})
                reasoning = msg.get("reasoning") or msg.get("thinking") or ""
                if reasoning:
                    data["_extracted_reasoning"] = reasoning
                return data
            except requests.exceptions.RequestException as e:
                if attempt < max_attempts - 1:
                    if self._debug_level() >= 1:
                        console.print(f"[dim yellow]⚠️ API-Call fehlgeschlagen (attempt {attempt + 1}/{max_attempts}), retry in {retry_delay}s: {e}[/dim yellow]")
                    time.sleep(retry_delay)
                    continue
                return {"error": str(e)}

    def chat(self, user_message: str, image_b64: Optional[str] = None) -> str:
        with self._chat_lock:
            return self._chat_impl(user_message, image_b64)

    @staticmethod
    def _arg_is_path_like(key: str, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        k = str(key).lower()
        if any(t in k for t in ("path", "file", "dir", "folder", "target", "dest", "source", "src")):
            return True
        return ("/" in value) or ("\\" in value)

    @staticmethod
    def _truncate_preview(value: Any, budget: int, path_like: bool) -> str:
        s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        budget = max(budget, 8)
        marker = "(...)"
        if len(s) <= budget:
            return s
        keep = budget - len(marker)
        if path_like:
            return marker + s[-keep:]
        return s[:keep] + marker

    def _format_args_preview(self, args) -> str:
        total = int(self.config.get("tool_log_args_length", 120) or 120)
        if not isinstance(args, dict):
            s = json.dumps(args, ensure_ascii=False, default=str)
            return s[:total] + "(...)" if len(s) > total else s
        if not args:
            return ""
        per_min = int(self.config.get("tool_log_args_per_min", 18) or 18)
        items = list(args.items())
        per = max(per_min, total // len(items))
        parts = []
        for key, value in items:
            path_like = self._arg_is_path_like(key, value)
            parts.append(f"{key}: '{self._truncate_preview(value, per, path_like)}'")
        return " ".join(parts)

    def _chat_impl(self, user_message: str, image_b64: Optional[str] = None) -> str:
        history = self.session_manager.load_session(self.session_id)
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
                enrichment_ctx = self.rag_manager.enrich_context(
                    user_message, self.config,
                    prev_user_msg=prev_user_msg,
                    prev_asst_msg=prev_asst_msg) or ""
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
                    console.print(f"[dim red]RAG-Enrichment-Fehler: {e}[/dim red]")
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
        if vision_active and bool(self.config.get("vision_disable_tools", True)):
            tools = []
        api_messages = list(history)
        api_messages = self._inject_ephemeral_context(api_messages, ephemeral_suffix)
        session_tokens = self.session_manager.get_total_tokens(
            self.session_id, self.system_prompt, tools, override_history=api_messages)
        if session_tokens > self.compression_threshold:
            console.print(f"[dim yellow]⚠️  Session-Tokens ({session_tokens}) > Threshold ({self.compression_threshold}). auto-compression...[/dim yellow]")
            result = self.compress_history()
            console.print(f"[dim]{result}[/dim]")
            history = self.session_manager.load_session(self.session_id)
        max_tool_turns = self.config.get("max_tool_turns", 15)
        tool_turns = 0
        rescue_attempts = 0
        while True:
            if tool_turns >= max_tool_turns:
                warning = (
                    f"⚠️ Stop after {tool_turns} tool passes{'n' if tool_turns != 1 else ''} "
                    f"(Limit: {max_tool_turns}) – the model wanted additional tools "
                    "Execute (possible loop). Refine the query or start /new.")
                history.append({"role": "assistant", "content": warning})
                self.session_manager.save_session(self.session_id, history)
                return warning
            api_messages = list(history)
            api_messages = self._inject_ephemeral_context(api_messages, ephemeral_suffix)
            tools = self.tool_manager.get_active_tools()
            if vision_active and bool(self.config.get("vision_disable_tools", True)):
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
            start_time = time.time()
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
                error_msg = f"API-ERROR: {data['error']}"
                history.append({"role": "assistant", "content": error_msg})
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
                error_msg = "⚠️ The model returned an empty response. Please repeat the question or start /new."
                return error_msg
            assistant_entry = {"role": "assistant", "content": content}
            if tool_calls:
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
                    console.print(f"[dim yellow]⚠️  Session-Tokens ({session_tokens}) > Threshold ({self.compression_threshold}). Auto-Kompression...[/dim yellow]")
                    result = self.compress_history()
                    console.print(f"[dim]{result}[/dim]")
                    history = self.session_manager.load_session(self.session_id)
                    expanded_check = list(history)
                    session_tokens = self.session_manager.get_total_tokens(self.session_id, self.system_prompt, tools, override_history=expanded_check)
                history = self.session_manager.compress_history_pointers(self.session_id, history, self.offloading_enabled)
                history = self.session_manager.apply_tool_ttl(history)
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
                show_intermediate = bool(self.config.get("cli_show_intermediate_content", True))
                if show_intermediate:
                    display = self._strip_thinking(content)
                    self._print_answer(display)
                    console.print()
            memory_tools_used = False
            round_max_res = self.config.get("max_tool_result_length", 4000)
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
                        "cd": "🧷",
                        "weather": "🌤️",
                        "news_digest": "📰",
                        "brightness_ctl": "💡",
                        "lint_code": "🔎",}
                    emoji = emoji_map.get(name, "🔧")
                    args_preview = self._format_args_preview(args)
                    log_msg = f"{emoji} {name} {args_preview}"
                    console.print(f"[bold yellow]{escape(log_msg)}[/bold yellow]")
                    self._notify(log_msg)
                    result = self.tool_manager.execute_tool(name, args)
                    result_str = json.dumps(result, ensure_ascii=False, default=str)
                    MAX_RES = round_max_res
                    if len(result_str) > MAX_RES:
                        sm = self.session_manager
                        hint = "\n[... truncated]"
                        cached = False
                        n_chunks = 0
                        if self.offloading_enabled:
                            entry = sm._load_cache_entry(self.session_id, tc_id) or {}
                            entry.update({"name": name, "arguments": func.get("arguments", "{}"), "result": result_str})
                            n_chunks = sm.save_chunked_cache(
                                self.session_id, tc_id, entry,
                                chunk_size=self.config.get("read_cache_chunk_size", 2500))
                            cached = True
                            if n_chunks:
                                hint = (f"\n[... truncated ({n_chunks} chunks), use read_cache with path={sm._cache_file(self.session_id, tc_id)} and chunk=0..{n_chunks - 1}]")
                            else:
                                hint = f"\n[... truncated, use read_cache with path={sm._cache_file(self.session_id, tc_id)} for full output]"
                        dbg = self._debug_level()
                        if dbg == 1:
                            console.print("[dim red]✂️[/dim red]")
                        elif dbg == 2:
                            cache_info = (f" | Cache: {sm._cache_file(self.session_id, tc_id)} ({n_chunks} chunks)" if cached else " | no cache (offloading off)")
                            console.print(f"[dim red]DEBUG ✂️ {name}: {len(result_str)} → {MAX_RES} Zeichen{cache_info}[/dim red]")
                        result_str = result_str[:MAX_RES] + hint
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": name,
                        "content": result_str})
                    self.session_manager.save_session(self.session_id, history)
            if self.offloading_enabled:
                history = self.session_manager.compress_history_pointers(
                    self.session_id, history, self.offloading_enabled)
                self.session_manager.save_session(self.session_id, history)
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

    def _call_meta_api(self, messages: List[Dict[str, Any]], max_tokens: int = 400):
        meta_max = int(self.config.get("meta_model_max_tokens", 0) or 0)
        if meta_max > 0:
            max_tokens = meta_max
        data = self._post_chat(
            base=self.config.get("meta_model_url", ""),
            model=self.config.get("meta_model_name", "meta"),
            api_key=self.config.get("meta_api_key", "") or self.api_key,
            timeout=int(self.config.get("meta_model_timeout", 60)),
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.2,
            label="meta")
        if data:
            return data
        if bool(self.config.get("meta_fallback_enabled", True)):
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

    @staticmethod
    def _tool_result_status(res_raw: str) -> str:
        try:
            res = json.loads(res_raw)
        except Exception:
            return (res_raw[:100] or "No results")
        if isinstance(res, dict):
            if res.get("error"):
                return f"ERROR: {str(res['error'])[:100]}"
            ok = res.get("success", res.get("status", ""))
            extra = f" rc={res['returncode']}" if "returncode" in res else ""
            msg = str(res.get("message") or res.get("output") or "")[:100]
            return f"{'ok' if ok else '?'}{extra} {msg}".strip()
        return res_raw[:100]

    def _extract_tool_trace(self, history: List[Dict[str, Any]]) -> str:
        start = 0
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("role") == "user":
                start = i + 1
                break
        tool_results = {}
        for m in history[start:]:
            if m.get("role") == "tool":
                tool_results[m.get("tool_call_id", "")] = m.get("content", "")
        lines = []
        for m in history[start:]:
            if m.get("role") != "assistant" or not m.get("tool_calls"):
                continue
            for tc in m.get("tool_calls", []):
                if tc.get("type") != "function":
                    continue
                func = tc.get("function", {})
                name = func.get("name", "?")
                try:
                    args = json.loads(func.get("arguments", "{}"))
                except Exception:
                    args = {}
                args_prev = json.dumps(args, ensure_ascii=False)[:80]
                status = self._tool_result_status(tool_results.get(tc.get("id", ""), ""))
                lines.append(f"- {name}({args_prev}): {status}")
        return "\n".join(lines) if lines else "(keine Tool-Calls)"

    @staticmethod
    def _last_tool_pairs(history: List[Dict[str, Any]], keep: int) -> List[Dict[str, Any]]:
        if keep <= 0:
            return []
        tail: List[Dict[str, Any]] = []
        i = len(history) - 1
        groups = 0
        while i >= 0 and groups < keep:
            if history[i].get("role") == "tool":
                j = i
                while j >= 0 and history[j].get("role") == "tool":
                    j -= 1
                if j >= 0 and history[j].get("role") == "assistant" and history[j].get("tool_calls"):
                    tail = history[j:i + 1] + tail
                    groups += 1
                    i = j - 1
                else:
                    i -= 1
            else:
                i -= 1
        return tail

    def _meta_char_budget(self, max_out_tokens: int) -> int:
        try:
            meta_ctx = int(self.config.get("meta_context_size", 4096) or 4096)
        except (TypeError, ValueError):
            meta_ctx = 4096
        instruction_tokens = 150
        safety = int(meta_ctx * 0.1)
        input_tokens = max(500, meta_ctx - max_out_tokens - instruction_tokens - safety)
        return int(input_tokens * 3)

    def _condense_chunk(self, chunk: str, max_out: int, part_label: str = "") -> Optional[str]:
        prompt = (
            "Condense this agent's tool trace into a concise "
            "progress report (max. 150 words). Include: goal, completed steps "
            "(files/commands/results), open issues, next step."
            + (f" This is part {part_label} of a longer trace." if part_label else "")
            + "\nTRACE:\n" + chunk)
        data = self._call_meta_api([{"role": "user", "content": prompt}],
                                   max_tokens=max_out)
        if data:
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content and content.strip():
                return content.strip()
        return None

    def _merge_partials(self, merged: str, max_out: int) -> Optional[str]:
        prompt = (
            "Merge these partial progress reports of ONE ongoing task into a "
            "single concise progress report (max. 150 words). Include: goal, "
            "completed steps, open issues, next step.\nPARTIALS:\n" + merged)
        data = self._call_meta_api([{"role": "user", "content": prompt}],
                                   max_tokens=max_out)
        if data:
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content and content.strip():
                return content.strip()
        return None

    def _condense_trace_meta(self, trace: str) -> Optional[str]:
        try:
            max_out = int(self.config.get("meta_summary_max_tokens", 300) or 300)
        except (TypeError, ValueError):
            max_out = 300
        budget = self._meta_char_budget(max_out)
        partials = []
        for i in range(0, len(trace), budget):
            condensed = self._condense_chunk(
                trace[i:i + budget], max_out,
                part_label=f"{len(partials) + 1}")
            if condensed:
                partials.append(condensed)
        if not partials:
            return None
        if len(partials) == 1:
            return partials[0]
        while len(partials) > 1:
            merged = "\n".join(f"- {p}" for p in partials)
            if len(merged) <= budget:
                return self._merge_partials(merged, max_out) or partials[0]
            new_partials = []
            window, size = [], 0
            for p in partials:
                cost = len(p) + 3
                if size + cost > budget and window:
                    new_partials.append(
                        self._merge_partials(
                            "\n".join(f"- {x}" for x in window), max_out)
                        or window[0])
                    window, size = [], 0
                window.append(p)
                size += cost
            if window:
                new_partials.append(
                    self._merge_partials(
                        "\n".join(f"- {x}" for x in window), max_out)
                    or window[0])
            partials = new_partials
        return partials[0]

    def _rescue_context(self, history: List[Dict[str, Any]], attempt: int):
        user_idx = 0
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("role") == "user":
                user_idx = i
                break
        trace = self._extract_tool_trace(history)
        prog_rel = ""
        try:
            prog_dir = os.path.join(self.tool_manager.workdir, ".progress")
            os.makedirs(prog_dir, exist_ok=True)
            prog_path = os.path.join(prog_dir, f"{self.session_id}.md")
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(prog_path, "a", encoding="utf-8") as f:
                f.write(f"\n## Rescue #{attempt} ({ts})\n{trace}\n")
            prog_rel = f".progress/{self.session_id}.md"
        except Exception:
            pass
        summary = trace
        mode = self.config.get("loop_rescue_mode", "hybrid")
        if mode in ("llm", "hybrid") and len(trace) > 600:
            condensed = self._condense_trace_meta(trace)
            if condensed:
                summary = condensed
        tail = self._last_tool_pairs(history, int(self.config.get("loop_rescue_keep_last", 1)))
        progress_msg = {
            "role": "user",
            "content": (
                f"[SYSTEM NOTE / PROGRESS] Current status of the ongoing task (Rescue #{attempt}):\n"
                f"{summary}\n"
                + (f"Details available upon request: read_file {prog_rel}\n" if prog_rel else "")
                + "Arbeite ab hier weiter. Wiederhole erledigte Schritte NICHT.")}
        new_history = history[:user_idx + 1] + [progress_msg] + tail
        self.session_manager.save_session(self.session_id, new_history)
        return new_history, f"Context-Rescue #{attempt}: {len(history)} → {len(new_history)} Messages"

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
            f"\n[BUDGET] Kontext {pct}% belegt (~{free // 1000}k frei). "
            f"Bei >80%: abschließen statt neu beginnen.")
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
        changed = False
        last_user_idx = -1
        if keep_last_user:
            for i in range(len(history) - 1, -1, -1):
                if history[i].get("role") == "user":
                    last_user_idx = i
                    break
        for i, msg in enumerate(history):
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            if i == last_user_idx:
                continue
            if not any(item.get("type") == "image_url" for item in msg["content"]):
                continue
            text = " ".join(item.get("text", "") for item in msg["content"] if item.get("type") == "text").strip()
            msg["content"] = (text or "(image)") + "\n[IMAGE_ANALYZED]"
            changed = True
        return changed

    def get_session_id(self) -> str:
        return self.session_id

    def get_total_tokens(self) -> int:
        tools = self.tool_manager.get_active_tools()
        return self.session_manager.get_total_tokens(self.session_id, self.system_prompt, tools)

    def start_new_session(self):
        self.tool_manager.reset_cwd()
        self.session_id = self.session_manager.create_session(self._build_system_prompt())
        self.notifications.clear()

    def switch_to(self, session_id: str):
        self.tool_manager.reset_cwd()
        if os.path.exists(p("data", "sessions", f"{session_id}.json")):
            self.session_id = session_id
            history = self.session_manager.load_session(session_id)
            if history:
                history[0]["content"] = self._build_system_prompt()
                self.session_manager.save_session(session_id, history)
            self._add_continuation_marker(session_id)
        else:
            raise ValueError(f"Session {session_id} not found.")

    def clear_current(self):
        self.session_manager.clear_session(self.session_id, self._build_system_prompt())
        self.notifications.clear()

    def compress_history(self) -> str:
        result = self.session_manager.compress_history(self.session_id, rag_offload=False)
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

    def get_active_tools_info(self) -> List[Dict[str, Any]]:
        tools = []
        for tool in self.tool_manager.available_tools:
            func = tool.get("function", {})
            tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "active": func.get("name", "") in self.tool_manager.active_tools})
        return tools


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

class ChatInterface:
    def __init__(self):
        self.agent = AgentCore(enable_tts=False, notifier=self._on_notification)
        self.session = PromptSession()
        self.scheduler = TaskScheduler(config=self.agent.config)
        self.agent.scheduler = self.scheduler
        self.agent.tool_manager.scheduler = self.scheduler
        self._status_message = ""
        self._status_until = 0
        self._active_status = None
    async def _scheduler_loop(self):
        interval = self.scheduler.check_interval
        while True:
            try:
                await asyncio.sleep(interval)
                if not self.scheduler.enabled:
                    continue
                def local_send(response: str, task: Dict[str, Any]):
                    task_id = task.get("id", "?")
                    console.print(f"[dim]⏰ Scheduler-Task {task_id}[/dim]")
                    try:
                        self.agent._print_assistant(response, title="Scheduler")
                    except Exception:
                        console.print(response)
                await asyncio.to_thread(
                    process_due_tasks,
                    self.agent,
                    self.scheduler,
                    "cli",
                    local_send)
            except asyncio.CancelledError:
                break
            except Exception as e:
                console.print(f"[dim red]Scheduler-Fehler: {e}[/dim red]")

    def _on_notification(self, msg: str):
        if any(x in msg for x in ("💉", "🧠", "🔔", "🔕", "🔊", "🔇", "🤔", "💭", "💾")):
            self._set_status(msg, 4.0)
#        if self._active_status is not None:
#            try:
#                self._active_status.update(f"[bold cyan]{msg}[/bold cyan]")
#            except Exception:
#                pass

    def _get_bottom_toolbar(self):
        now = time.time()
        tokens = getattr(self.agent, "last_session_tokens", 0)
        if tokens is None or tokens < 0:
            tokens = 0
        ctx = self.agent.context_size or 0
        pct = int((tokens / ctx) * 100) if ctx else 0
        tps = f"{self.agent.last_tps:.1f} t/s" if getattr(self.agent, "last_tps", None) else ""
        status = f" {self._status_message} |" if now < self._status_until else ""
        if pct > 85:
            style = "bg:ansired #ffffff"
        elif pct > 65:
            style = "bg:ansiyellow #000000"
        else:
            style = "bg:#1f2937 #9ca3af"
        text = f"{status} Tokens: {tokens}/{ctx} ({pct}%)"
        if tps:
            text += f" | {tps}"
        return [(style, text)]

    def _set_status(self, msg: str, duration: float = 3.0):
        self._status_message = msg
        self._status_until = time.time() + duration

    async def run(self):
        ######################_clear_#####################
        #os.system('clear' if os.name != 'nt' else 'cls')#
        ##################################################
        console.print(Panel("[bold green]Vishva Chat[/bold green]\n" "Type /help for commands.", border_style="green"))
        scheduler_task = None
        if self.scheduler.enabled:
            scheduler_task = asyncio.create_task(self._scheduler_loop())
        console.print(f"[dim]Session: {self.agent.get_session_id()}[/dim]")
        if self.agent.startup_tokens >= 0:
            console.print(f"[dim]📊 Session Tokens: {self.agent.startup_tokens}[/dim]")
        else:
            console.print("[dim yellow]📊 Session Tokens: (Calculation failed)[/dim yellow]")
        try:
            width = os.get_terminal_size().columns
        except OSError:
            width = 50
        console.print("-" * width)
        while True:
            try:
                user_input = (await self.session.prompt_async(
                    HTML('<prompt_color>🧘>> </prompt_color>'),
                    completer=CommandCompleter(),
                    style=Style.from_dict({'prompt_color': 'cyan'}),
                    bottom_toolbar=self._get_bottom_toolbar)).strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[bold yellow]bye 👋🏻[/bold yellow]")
                break
            if not user_input:
                continue
            if user_input.startswith('/'):
                parts = user_input.split(' ', 1)
                cmd = parts[0]
                arg = parts[1] if len(parts) > 1 else None
                if cmd == '/exit':
                    console.print("[bold yellow]bye 👋🏻[/bold yellow]")
                    break
                if cmd == '/bye':
                    console.print("[bold yellow]bye 👋🏻[/bold yellow]")
                    break
                elif cmd == '/new':
                    self.agent.start_new_session()
                    console.print(f"[bold green]✨ new session started: {self.agent.get_session_id()}[/bold green]")
                    try:
                        console.print(f"[dim]📊 Session Tokens: {self.agent.get_total_tokens()}[/dim]")
                    except Exception:
                        pass
                elif cmd == '/sched':
                    if not hasattr(self.agent, "scheduler"):
                        console.print("[bold red]❌ Scheduler nicht initialisiert.[/bold red]")
                        continue
                    if not arg:
                        tasks = self.agent.scheduler.list_tasks()
                        if not tasks:
                            console.print("[yellow]Keine Scheduler-Tasks vorhanden.[/yellow]")
                        else:
                            console.print("[bold cyan]Scheduler-Tasks:[/bold cyan]")
                            for t in tasks:
                                console.print(
                                    f"  • {t.get('id')} | {t.get('trigger_time')} | "
                                    f"target={t.get('target') or 'auto'} | "
                                    f"status={t.get('status')} | "
                                    f"{str(t.get('prompt', ''))[:60]}")
                        console.print("[dim]   Usage: /sched add <YYYY-MM-DD HH:MM:SS> <prompt>[/dim]")
                        console.print("[dim]          /sched cancel <id>[/dim]")
                        continue
                    parts = arg.split(maxsplit=1)
                    sub = parts[0].lower()
                    if sub == "add":
                        if len(parts) < 2:
                            console.print("[bold red]❌ Usage: /sched add <YYYY-MM-DD HH:MM:SS> <prompt>[/bold red]")
                            continue
                        rest = parts[1]
                        time_parts = rest.split(maxsplit=2)
                        if len(time_parts) < 3:
                            console.print("[bold red]❌ Usage: /sched add <YYYY-MM-DD HH:MM:SS> <prompt>[/bold red]")
                            continue
                        date_str = time_parts[0]
                        time_str = time_parts[1]
                        prompt = time_parts[2]
                        trigger_time = f"{date_str} {time_str}"
                        result = self.agent.scheduler.add_task(
                            chat_id=None,
                            trigger_time=trigger_time,
                            prompt=prompt,
                            target="cli")
                        if result.get("success"):
                            console.print(f"[bold green]✅ {result.get('message')}[/bold green]")
                        else:
                            console.print(f"[bold red]❌ {result.get('error')}[/bold red]")
                    elif sub == "cancel":
                        if len(parts) < 2:
                            console.print("[bold red]❌ Usage: /sched cancel <id>[/bold red]")
                            continue
                        task_id = parts[1].strip()
                        result = self.agent.scheduler.cancel_task(task_id)
                        if result.get("success"):
                            console.print(f"[bold green]✅ {result.get('message')}[/bold green]")
                        else:
                            console.print(f"[bold red]❌ {result.get('error')}[/bold red]")
                    else:
                        console.print("[bold red]❌ Usage: /sched | /sched add ... | /sched cancel <id>[/bold red]")
                elif cmd == '/session':
                    if not arg:
                        sessions = self.agent.session_manager.list_sessions()
                        if sessions:
                            sessions_with_time = []
                            for s in sessions:
                                path = p("data", "sessions", f"{s}.json")
                                if os.path.exists(path):
                                    mtime = os.path.getmtime(path)
                                    mtime_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                                else:
                                    mtime = 0
                                    mtime_str = "unknown"
                                sessions_with_time.append((s, mtime, mtime_str))
                            sessions_with_time.sort(key=lambda x: x[1])
                            current = self.agent.get_session_id()
                            console.print(f"[bold cyan]available sessions ({len(sessions_with_time)}):[/bold cyan]")
                            for s, _, ts in sessions_with_time:
                                marker = " ← [green]aktiv[/green]" if s == current else ""
                                console.print(f"  • [bold]{s}[/bold] (Zugriff: {ts}){marker}")
                            console.print("[dim]   /session <id> to change[/dim]")
                        else:
                            console.print("[yellow]No sessions available.[/yellow]")
                    else:
                        try:
                            self.agent.switch_to(arg)
                            console.print(f"[bold cyan]🔄 session loaded: {arg}[/bold cyan]")
                            try:
                                console.print(f"[dim]📊 Session Tokens: {self.agent.get_total_tokens()}[/dim]")
                            except Exception:
                                pass
                        except ValueError as e:
                            console.print(f"[bold red]{e}[/bold red]")
                elif cmd == '/clear':
                    self.agent.clear_current()
                    console.print("[bold yellow]🧹 history deleted.[/bold yellow]")
                elif cmd == '/history':
                    for m in self.agent.session_manager.load_session(self.agent.get_session_id()):
                        role = m['role']
                        content = m.get('content', '')
                        if role == 'system':
                            console.print(f"[bold magenta]{role.upper()}[/bold magenta]: {content[:80]}...")
                        elif role == 'user':
                            console.print(f"[bold blue]{role.upper()}[/bold blue]: {content}")
                        elif role == 'assistant':
                            has_tools = "🔧" if m.get('tool_calls') else ""
                            console.print(f"[bold green]{role.upper()}[/bold green]{has_tools}: {content[:200]}")
                        elif role == 'tool':
                            console.print(f"[bold yellow]{role.upper()}[/bold yellow]: {content[:100]}")
                elif cmd == '/tokens':
                    console.print(f"🔢 Tokens: {self.agent.get_total_tokens()}")
                elif cmd == '/threshold':
                    ctx = self.agent.context_size
                    thr = self.agent.compression_threshold
                    source = "config.json" if self.agent.config.get("compression_threshold") is not None else "auto (ctx_size - reserve)"
                    console.print(f"[bold cyan]📊 Compression Threshold[/bold cyan]\n  Value: {thr}\n  Context Size: {ctx}\n  Source: {source}")
                elif cmd == '/config':
                    console.print(Panel(json.dumps(self.agent.config, indent=2, ensure_ascii=False), title="Config"))
                elif cmd == '/voice':
                    if not self.agent.tts_manager:
                        console.print("[bold red]❌ TTS nicht verfügbar.[/bold red]")
                    elif not arg:
                        status = "AN" if self.agent.tts_manager.enabled else "AUS"
                        console.print(f"[bold yellow]🔊 Voice Status: {status}[/bold yellow]")
                        if status == "AN":
                            self._set_status(f"🔊", 2)
                        elif status == "AUS":
                            self._set_status(f"🔇", 2)
                    elif arg in ("on", "off"):
                        self.agent.tts_manager.toggle(arg == "on")
                        status = "AN" if self.agent.tts_manager.enabled else "AUS"
                        if status == "AN":
                            self._set_status(f"🔊", 2)
                        elif status == "AUS":
                            self._set_status(f"🔇", 2)
                    else:
                        console.print("[bold red]❌ Usage: /voice [on|off][/bold red]")
                elif cmd == '/info':
                    sid = self.agent.get_session_id()
                    tokens = self.agent.get_total_tokens()
                    session_path = p("data", "sessions", f"{sid}.json")
                    created_at = "unknown"
                    last_access = "unknown"
                    if os.path.exists(session_path):
                        stat = os.stat(session_path)
                        created_at = datetime.datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M:%S")
                        last_access = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                    active = [t['function']['name'] for t in self.agent.tool_manager.get_active_tools()]
                    if self.agent.rag_manager:
                        rag_count = len(self.agent.rag_manager.entries)
                        rag_info = f"RAG-Entries: {rag_count}"
                    else:
                        rag_info = "RAG: deaktiviert"
                    info_text = f"""[bold cyan]Session Info[/bold cyan]
ID:           [bold]{sid}[/bold]
Tokens:       {tokens}
Created:      {created_at}
Last accessed:{last_access}
Aktiv Tools: {', '.join(active) if active else 'Keine'}
{rag_info}"""
                    console.print(info_text)
                elif cmd in ("/zip", "/shrink"):
                    result = self.agent.compress_history()
                    console.print(f"[bold cyan]{result}[/bold cyan]")
                elif cmd == '/listtools':
                    try:
                        tools = self.agent.get_active_tools_info()
                        tools.sort(key=lambda t: (
                            0 if t.get("active") else 1,
                            str(t.get("name", "")).lower()))
                        lines = []
                        for t in tools:
                            state = "🟢" if t.get("active") else "🔴"
                            lines.append(f"{state} {t.get('name', '?')}")
                        print("\n".join(lines) if lines else "Keine Tools")
                    except Exception as e:
                        print("Error", str(e))
                elif cmd == '/offloading':
                    if not arg:
                        status = "AN" if self.agent.offloading_enabled else "AUS"
                        console.print(f"[bold yellow]💾 Offloading: {status}[/bold yellow]")
                        self._set_status(f" Offloading: {status}", 2)
                    elif arg in ("on", "off"):
                        self.agent.offloading_enabled = (arg == "on")
                        self.agent.config["offloading_enabled"] = self.agent.offloading_enabled
                        self.agent._save_config()
                        status = "AN" if self.agent.offloading_enabled else "AUS"
                        console.print(f"[bold yellow]💾 Offloading: {status}[/bold yellow]")
                        self._set_status(f"Offloading: {status}", 2)
                        if self.agent.offloading_enabled:
                            console.print("[dim]   Tool-Args/Results werden in Cache ausgelagert.[/dim]")
                        else:
                            console.print("[dim]   Tool-Args/Results bleiben in der History.[/dim]")
                    else:
                        console.print("[bold red]❌ Usage: /offloading [on|off][/bold red]")
                elif cmd == '/sound':
                    if not arg:
                        status = "AN" if self.agent.sound_enabled else "AUS"
                        console.print(f"[bold yellow]🔔 Sound: {status}[/bold yellow]")
                        if status == "AN":
                            self._set_status(f"🔔", 2)
                        elif status == "AUS":
                            self._set_status(f"🔕", 2)
                    elif arg in ("on", "off"):
                        self.agent.sound_enabled = (arg == "on")
                        self.agent.config["sound_enabled"] = self.agent.sound_enabled
                        self.agent._save_config()
                        status = "AN" if self.agent.sound_enabled else "AUS"
                        console.print(f"[bold yellow] Sound: {status}[/bold yellow]")
                        if status == "AN":
                            self._set_status(f"🔔", 2)
                        elif status == "AUS":
                            self._set_status(f"🔕", 2)
                    else:
                        console.print("[bold red]❌ Usage: /sound [on|off][/bold red]")
                elif cmd == '/showThinking':
                    if not arg:
                        status = "AN" if self.agent.show_thinking else "AUS"
                        console.print(f"[bold yellow]🤔 Thinking: {status}[/bold yellow]")
                        self._set_status(f" Think: {status}", 2)
                    elif arg in ("on", "off"):
                        self.agent.show_thinking = (arg == "on")
                        self.agent.config["show_thinking"] = self.agent.show_thinking
                        self.agent._save_config()
                        status = "AN" if self.agent.show_thinking else "AUS"
                        console.print(f"[bold yellow]🤔 Thinking: {status}[/bold yellow]")
                        self._set_status(f" Think: {status}", 2)
                        if self.agent.show_thinking:
                            console.print("[dim]   Modell-Thinking-Bloecke werden angezeigt.[/dim]")
                        else:
                            console.print("[dim]   Modell-Thinking-Bloecke werden ausgefiltert.[/dim]")
                    else:
                        console.print("[bold red]❌ Usage: /showThinking [on|off][/bold red]")

                elif cmd == '/image':
                    if not arg:
                        console.print("[bold red]❌ Usage: /image <path> [optionaler Text][/bold red]")
                    else:
                        parts = arg.split(' ', 1)
                        img_path = parts[0]
                        img_text = parts[1] if len(parts) > 1 else "Describe this picture."

                        if not bool(self.agent.config.get("vision_enabled", True)):
                            console.print(
                                "[bold red]❌ Vision is disabled. "
                                "Set vision_enabled=true in config.json.[/bold red]")
                            continue
                        b64_url = self.agent._process_image(img_path)
                        if b64_url:
                            console.print(f"[bold green]🖼️ Image processed:{img_path}[/bold green]")
                            console.print("[bold cyan]Analyzing...[/bold cyan]")
                            try:
                                response = await asyncio.to_thread(
                                    self.agent.chat,
                                    img_text,
                                    b64_url)
                                if response:
                                    self.agent._print_answer(response)
                                    if self.agent.tts_manager and self.agent.tts_manager.enabled:
                                        self.agent.tts_manager.speak(response)
                            except Exception as e:
                                console.print(f"[bold red]Error:[/bold red] {str(e)}")
                        else:
                            console.print(
                                f"[bold red]❌ Image not found or invalid: {img_path}[/bold red]")
                elif cmd == '/stt':
                    import tempfile
                    import shutil
                    import signal
                    recorder = None
                    if shutil.which("arecord"):
                        recorder = "arecord"
                    elif shutil.which("sox") or shutil.which("rec"):
                        recorder = "rec"
                    if not recorder:
                        console.print("[bold red]❌ No audio recorder found. Install alsa-utils (arecord) or sox (rec).[/bold red]")
                        continue
                    tmp_dir = tempfile.mkdtemp()
                    tmp_wav = os.path.join(tmp_dir, "recording.wav")
                    console.print(f"[bold cyan]🎙️ Recording in progress... Press ENTER to stop.[/bold cyan]")
                    if recorder == "arecord":
                        proc = subprocess.Popen(
                            ["arecord", "-f", "cd", tmp_wav],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
                    else:
                        proc = subprocess.Popen(
                            ["rec", "-c", "1", "-r", "16000", "-b", "16", tmp_wav],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
                    try:
                        input()
                    except (EOFError, KeyboardInterrupt):
                        pass
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    if not os.path.exists(tmp_wav) or os.path.getsize(tmp_wav) == 0:
                        console.print("[bold red]❌ Recording failed or empty.[/bold red]")
                        continue
                    console.print("[bold cyan]📝 transcribe...[/bold cyan]")
                    stt_cmd = self.agent.config.get("stt_command", "")
                    if not stt_cmd:
                        if shutil.which("whisper"):
                            stt_cmd = "whisper {audio} --model tiny --language German --output_format txt --output_dir {dir}"
                        else:
                            console.print("[bold red]❌ No STT configured. Install whisper or set ‘stt_command’ in config.json.[/bold red]")
                            continue
                    txt_dir = tempfile.mkdtemp()
                    formatted_cmd = stt_cmd.format(audio=tmp_wav, dir=txt_dir)
                    transcript = ""
                    try:
                        result = subprocess.run(formatted_cmd, shell=True, capture_output=True, text=True, timeout=60)
                        txt_file = os.path.join(txt_dir, os.path.basename(tmp_wav).replace(".wav", ".txt"))
                        if os.path.exists(txt_file):
                            with open(txt_file, "r", encoding="utf-8") as f:
                                transcript = f.read().strip()
                        else:
                            transcript = result.stdout.strip()
                    except Exception as e:
                        console.print(f"[bold red]❌ STT-Fehler: {e}[/bold red]")
                        continue
                    finally:
                        try:
                            os.remove(tmp_wav)
                            shutil.rmtree(txt_dir, ignore_errors=True)
                            shutil.rmtree(tmp_dir, ignore_errors=True)
                        except:
                            pass
                    if not transcript:
                        console.print("[yellow]⚠️ No transcript was provided.[/yellow]")
                        continue
                    console.print(f"[bold green]📝 Transkript:[/bold green] {transcript}")
                    try:
                        response = await asyncio.to_thread(self.agent.chat, transcript)
                        if response:
                            self.agent._print_answer(response)
                            if self.agent.tts_manager and self.agent.tts_manager.enabled:
                                self.agent.tts_manager.speak(response)
                    except Exception as e:
                        console.print(f"[bold red]Error:[/bold red] {str(e)}")
                elif cmd == '/personality':
                    if not arg:
                        personas_dir = p("personas")
                        if os.path.exists(personas_dir):
                            files = sorted([f.replace(".md", "") for f in os.listdir(personas_dir) if f.endswith(".md")])
                            console.print("[bold cyan]available personas:[/bold cyan]")
                            for persona in files:
                                console.print(f"  • {persona}")
                            console.print("[dim]   /personality <name> to switch[/dim]")
                        else:
                            console.print("[bold red]❌ personas/ directory not found.[/bold red]")
                    else:
                        result = self.agent.set_personality(arg)
                        if result.get("success"):
                            console.print(f"[bold green]✅ {result['message']}[/bold green]")
                        else:
                            console.print(f"[bold red]❌ {result['error']}[/bold red]")
                elif cmd == '/persona':
                    if not arg:
                        personas_dir = p("personas")
                        if os.path.exists(personas_dir):
                            files = sorted([f.replace(".md", "") for f in os.listdir(personas_dir) if f.endswith(".md")])
                            console.print("[bold cyan]Verfügbare Persönlichkeiten:[/bold cyan]")
                            for persona in files:
                                console.print(f"  • {persona}")
                            console.print("[dim]   /personality <name> zum Wechseln[/dim]")
                        else:
                            console.print("[bold red]❌ personas/ directory not found.[/bold red]")
                    else:
                        result = self.agent.set_personality(arg)
                        if result.get("success"):
                            console.print(f"[bold green]✅ {result['message']}[/bold green]")
                        else:
                            console.print(f"[bold red]❌ {result['error']}[/bold red]")
                elif cmd == '/frame':
                    frame_on = self.agent.config.get("cli_frame_enabled", True)
                    if not arg:
                        status = "AN" if frame_on else "AUS"
                        console.print(f"[bold yellow]🖼️ Rahmen: {status}[/bold yellow]")
                        console.print("[dim]   Usage: /frame [on|off][/dim]")
                    elif arg in ("on", "off"):
                        frame_on = (arg == "on")
                        self.agent.config["cli_frame_enabled"] = frame_on
                        self.agent._save_config()
                        status = "AN" if frame_on else "AUS"
                        console.print(f"[bold yellow]🖼️ Rahmen: {status}[/bold yellow]")
                        if frame_on:
                            console.print("[dim]   Antworten werden in einem Panel-Rahmen angezeigt.[/dim]")
                        else:
                            console.print("[dim]   Antworten werden ohne Rahmen angezeigt.[/dim]")
                    else:
                        console.print("[bold red]❌ Usage: /frame [on|off][/bold red]")
                elif cmd == '/help':
                    help_text = """
[bold blue]Verfügbare Commands:[/bold blue]
/exit              – Beenden
/new               – Neue Session starten
/session <id>      – Zu Session wechseln
/clear             – Aktuelle History löschen
/history           – History anzeigen
/tokens            – Token-Anzahl (tiktoken)
/config            – Config anzeigen
/voice [on|off]            – Sprachausgabe steuern
/stt <seconds>     – Spracheingabe aufnehmen und transkribieren
/personality <name>– Persönlichkeit wechseln
/persona <name>    – Persönlichkeit wechseln
/offloading [on|off]       – Tool-Offloading toggeln
/sound [on|off]            – Benachrichtigungston toggeln
/showThinking [on|off]     – Thinking-Bloecke anzeigen
/image <path>      – Bild analysieren (Vision-Modell)
/threshold         – Aktuellen Compression-Threshold anzeigen
/help              – Diese Hilfe
"""
                    console.print(help_text)
                else:
                    console.print(f"[bold red]Unbekanntes Command: {cmd}[/bold red]")
                continue
            self._set_status("💭", 0)
            response = None
            with console.status("[bold cyan]💭Thinking…[/bold cyan]", spinner="dots") as status:
                self._active_status = status
                self.agent._active_status = status
                try:
                    response = await asyncio.to_thread(self.agent.chat, user_input)
                except Exception as e:
                    console.print(f"[bold red]Error:[/bold red] {str(e)}")
                finally:
                    self._active_status = None
                    self.agent._active_status = None

            if response is None:
                continue
            if self._status_message.startswith("💭"):
                self._status_message = ""

            if self.agent.sound_enabled:
                _play_notification_sound()
            if response:
                self.agent._print_answer(response)
                if self.agent.tts_manager and self.agent.tts_manager.enabled:
                    self.agent.tts_manager.speak(response)
            else:
                console.print("[yellow]Agent lieferte eine leere Antwort.[/yellow]")
            try:
                width = os.get_terminal_size().columns
            except OSError:
                width = 50
            console.print("-" * width)

async def main():
    interface = ChatInterface()
    await interface.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[bold yellow]bye 👋🏻[/bold yellow]")
