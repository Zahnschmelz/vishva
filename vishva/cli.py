import os
#import io
#import re
#import sys
#import copy
import time
import json
#import string
import shutil
#import base64
#import random
import asyncio
#import requests
import datetime
#import threading
import subprocess
from rich.panel import Panel
#from rich.markup import escape
#from .paths import p, cfg_path
from .paths import p
from .agent import AgentCore
from rich.console import Console
#from rich.markdown import Markdown
#from .tool_manager import ToolManager
from prompt_toolkit.styles import Style
from prompt_toolkit import PromptSession
#from .session_manager import SessionManager
from prompt_toolkit.formatted_text import HTML
#from .tts_manager import TTSManager as TTSManager
#from .code_manager import CodeManager, CODE_BLOCK_RE, LANG_TO_EXT
#from typing import Dict, List, Any, Optional, Callable
from typing import Dict, Any
from .scheduler import TaskScheduler, process_due_tasks
from prompt_toolkit.completion import Completer, Completion

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
            '/stt', '/personality', '/persona', '/frame', '/context']
        for cmd in commands:
            if cmd.startswith(text):
                yield Completion(cmd, start_position=-len(text))

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
                console.print(f"[dim red]Scheduler-Failure: {e}[/dim red]")

    def _on_notification(self, msg: str):
        if any(x in msg for x in ("💉", "🧠", "🔔", "🔕", "🔊", "🔇", "🤔", "💭", "💾")):
            self._set_status(msg, 4.0)

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
                                console.print(f"  • [bold]{s}[/bold] (Access: {ts}){marker}")
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
                elif cmd == '/zip':
                    result = self.agent.compress_history()
                    console.print(f"[bold cyan]{result}[/bold cyan]")
                elif cmd == '/shrink':
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
                        console.print("\n".join(lines) if lines else "Keine Tools")
                    except Exception as e:
                        console.print("Error", str(e))
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
                            console.print("[dim]   Tool-Args/Results are stored in the cache.[/dim]")
                        else:
                            console.print("[dim]   Tool-Args/Results  remain in history.[/dim]")
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
                            console.print("[dim]   Modell-Thinking-blocks are displayed.[/dim]")
                        else:
                            console.print("[dim]   Modell-Thinking-blocks are filtered out.[/dim]")
                    else:
                        console.print("[bold red]❌ Usage: /showThinking [on|off][/bold red]")

                elif cmd == '/image':
                    if not arg:
                        console.print("[bold red]❌ Usage: /image <path> [optionaler Text][/bold red]")
                    else:
                        parts = arg.split(' ', 1)
                        img_path = parts[0]
                        img_text = parts[1] if len(parts) > 1 else "describe this picture."

                        if not bool(self.agent.config.get("vision_enabled", True)):
                            console.print(
                                "[bold red]❌ Vision disabled. "
                                "set vision_enabled=true in config.json.[/bold red]")
                            continue
                        b64_url = self.agent._process_image(img_path)
                        if b64_url:
                            console.print(f"[bold green]🖼️ Image processed: {img_path}[/bold green]")
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
#                    import signal
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
                    console.print("[bold cyan]📝 Transcribe...[/bold cyan]")
                    stt_cmd = self.agent.config.get("stt_command", "")
                    if not stt_cmd:
                        if shutil.which("whisper"):
                            stt_cmd = "whisper {audio} --model tiny --language German --output_format txt --output_dir {dir}"
                        else:
                            console.print("[bold red]❌ No STT configured. Install whisper or set 'stt_command' in config.json.[/bold red]")
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
                        console.print(f"[bold red]❌ STT-Failure: {e}[/bold red]")
                        continue
                    finally:
                        try:
                            os.remove(tmp_wav)
                            shutil.rmtree(txt_dir, ignore_errors=True)
                            shutil.rmtree(tmp_dir, ignore_errors=True)
                        except:
                            pass
                    if not transcript:
                        console.print("[yellow]⚠️ Keine Transkription erhalten.[/yellow]")
                        continue
                    console.print(f"[bold green]📝 Transcript:[/bold green] {transcript}")
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
                            console.print("[bold cyan]Verfügbare Persönlichkeiten:[/bold cyan]")
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
                            console.print("[dim]   /personality <name> to switch[/dim]")
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
                            console.print("[dim]   Answers are displayed in a panel frame.[/dim]")
                        else:
                            console.print("[dim]   Replies are displayed without borders.[/dim]")
                    else:
                        console.print("[bold red]❌ Usage: /frame [on|off][/bold red]")
                elif cmd == '/context':
                    msgs = getattr(self.agent, "_last_api_messages", None)
                    if not msgs:
                        console.print("[yellow]⚠️ No API calls have been made in this session yet.[/yellow]")
                        continue
                    tools = getattr(self.agent, "_last_api_tools", []) or []
                    turn = getattr(self.agent, "_last_api_turn", "?")
                    tool_names = [t.get("function", {}).get("name", "?") for t in tools]
                    console.print(Panel(
                        f"[bold cyan]RAM Context — Most Recently Sent (Tool-Turn {turn})[/bold cyan]\n"
                        f"Messages: {len(msgs)} | Tools activ: {len(tools)}"
                        + (f" ({', '.join(tool_names)})" if tool_names else ""),
                        expand=False))
                    if arg == "json":
                        console.print(Panel(
                            json.dumps(msgs, indent=2, ensure_ascii=False),
                            title="RAW JSON"))
                        continue
                    role_style = {"system": "magenta", "user": "blue",
                                "assistant": "green", "tool": "yellow"}
                    for i, m in enumerate(msgs):
                        role = m.get("role", "?")
                        content = m.get("content", "")
                        if isinstance(content, list):
                            parts = []
                            for item in content:
                                if isinstance(item, dict):
                                    if item.get("type") == "text":
                                        parts.append(str(item.get("text", "")))
                                    elif item.get("type") == "image_url":
                                        parts.append("[IMAGE_B64]")
                            content = "\n".join(parts)
                        head = f"[bold {role_style.get(role, 'white')}]{i:02d} {role.upper()}[/bold {role_style.get(role, 'white')}]"
                        tcs = m.get("tool_calls")
                        if tcs:
                            names = ", ".join(tc.get("function", {}).get("name", "?") for tc in tcs)
                            head += f" [bold yellow]🔧 → {names}[/bold yellow]"
                        if m.get("tool_call_id"):
                            head += f" [dim](call_id={str(m['tool_call_id'])[:12]}…)[/dim]"
                        console.print(head)
                        console.print(str(content))
                        console.print()
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
/context [json]    – RAM-Kontext zeigen (exakt das, was zuletzt an die API ging)
/help              – Diese Hilfe
"""
                    console.print(help_text)
                else:
                    console.print(f"[bold red]Unknown Command: {cmd}[/bold red]")
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
                console.print("[yellow]agent gave noncommittal answer.[/yellow]")
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
        pass
