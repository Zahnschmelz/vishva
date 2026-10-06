import os
from datetime import datetime
import sys
import json
import asyncio
import subprocess
import shutil
import tempfile
import re
from typing import Dict
from .paths import p, cfg_path
from .scheduler import TaskScheduler, prepare_agent_for_scheduled_task
import queue
import threading
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, TypeHandler, ApplicationHandlerStop
from .agent import AgentCore
import html as html_mod
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler

agents: Dict[str, AgentCore] = {}
notification_queues: Dict[str, queue.Queue] = {}
try:
    with open(p("config", "config.json"), "r", encoding="utf-8") as f:
        _bot_cfg = json.load(f)
except Exception:
    _bot_cfg = {}
scheduler = TaskScheduler(config=_bot_cfg)

import threading
import asyncio

_bot_loop: asyncio.AbstractEventLoop = None
_bot_app = None
_pending_confirms: Dict[str, Dict] = {}
_confirm_lock = threading.Lock()


def set_bot_runtime(loop, app):
    global _bot_loop, _bot_app
    _bot_loop = loop
    _bot_app = app

async def _send_confirm_msg(chat_id, tool_name, preview, state):
    if not _bot_app:
        return
    text = (
        f"🔐 <b>Tool-Bestätigung erforderlich</b>\n"
        f"<b>Tool:</b> <code>{html_mod.escape(str(tool_name))}</code>\n"
        f"<b>Args:</b> <code>{html_mod.escape(str(preview)[:200])}</code>")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Allow (y)",
                              callback_data=f"toolconfirm|y|{chat_id}"),
         InlineKeyboardButton("🔁 Always (a)",
                              callback_data=f"toolconfirm|a|{chat_id}")],
        [InlineKeyboardButton("❌ Deny (n)",
                              callback_data=f"toolconfirm|n|{chat_id}"),
         InlineKeyboardButton("📝 Deny + Grund (r)",
                              callback_data=f"toolconfirm|r|{chat_id}")],])
    try:
        msg = await _bot_app.bot.send_message(
            chat_id=chat_id, text=text, reply_markup=keyboard,
            parse_mode="HTML")
        state["message_id"] = msg.message_id
    except Exception as e:
        print(f"[ToolConfirm] send failed: {e}")

async def handle_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"[CONFIRM] Callback empfangen: {query.data!r}", flush=True)
    try:
        await query.answer()
    except Exception as e:
        print(f"[CONFIRM] answer() fehlgeschlagen: {e!r}", flush=True)
    data = query.data or ""
    if not data.startswith("toolconfirm|"):
        return
    try:
        _, choice, chat_id = data.split("|", 2)
    except ValueError:
        return
    chat_id = str(chat_id)
    with _confirm_lock:
        state = _pending_confirms.get(chat_id)
    print(f"[CONFIRM] State für {chat_id!r}: {state!r}", flush=True)
    if not state:
        try:
            await query.edit_message_text("⚠️ Diese Bestätigung ist abgelaufen.")
        except Exception:
            pass
        return
    tool = state.get("tool", "?")
    if choice == "y":
        try:
            await query.edit_message_text(f"✅ Tool '{tool}' erlaubt.")
        except Exception:
            pass
        _finish_confirm(chat_id, {"allow": True})
    elif choice == "a":
        try:
            await query.edit_message_text(
                f"🔁 Tool '{tool}' für diese Session immer erlaubt.")
        except Exception:
            pass
        _finish_confirm(chat_id, {"allow": True, "always": True})
    elif choice == "n":
        try:
            await query.edit_message_text(f"❌ Tool '{tool}' abgelehnt.")
        except Exception:
            pass
        _finish_confirm(chat_id, {"allow": False, "reason": "user denied"})
    elif choice == "r":
        with _confirm_lock:
            st = _pending_confirms.get(chat_id)
            if st:
                st["stage"] = 2
        try:
            await query.edit_message_text(
                f"📝 Bitte antworte jetzt mit dem Grund für die Ablehnung von '{tool}':")
        except Exception:
            pass

def make_confirm_handler(chat_id: str):
    chat_id = str(chat_id)
    def handler(tool_name: str, preview: str) -> Dict:
        return _telegram_confirm(chat_id, tool_name, preview)
    return handler

async def _invalidate_confirm_msg(chat_id, message_id):
    if not _bot_app:
        return
    try:
        await _bot_app.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text="⏱️ Tool-Bestätigung abgelaufen (Timeout).")
    except Exception:
        pass

def _telegram_confirm(chat_id, tool_name, preview) -> Dict:
    if _bot_loop is None:
        return {"allow": True, "reason": ""}
    ev = threading.Event()
    state = {"event": ev, "result": None, "stage": 1,
             "tool": tool_name, "message_id": None}
    with _confirm_lock:
        _pending_confirms[chat_id] = state
    try:
        asyncio.run_coroutine_threadsafe(
            _send_confirm_msg(chat_id, tool_name, preview, state), _bot_loop)
    except Exception as e:
        with _confirm_lock:
            _pending_confirms.pop(chat_id, None)
        return {"allow": False, "reason": f"confirmation failed: {e}"}
    print(f"[CONFIRM] Warte auf Bestätigung: chat={chat_id!r}, tool={tool_name!r}", flush=True)
    done = ev.wait(timeout=300)
    with _confirm_lock:
        state = _pending_confirms.pop(chat_id, {})

    if not done or state.get("result") is None:
        msg_id = state.get("message_id")
        if msg_id and _bot_loop:
            try:
                asyncio.run_coroutine_threadsafe(
                    _invalidate_confirm_msg(chat_id, msg_id), _bot_loop)
            except Exception:
                pass
        return {"allow": False, "reason": "confirmation timeout"}
    return state["result"]


def _finish_confirm(chat_id, result):
    with _confirm_lock:
        st = _pending_confirms.get(chat_id)
        if st:
            st["result"] = result
            st["event"].set()

async def handle_confirm_reply(chat_id, text) -> bool:
    chat_id = str(chat_id)
    with _confirm_lock:
        state = _pending_confirms.get(chat_id)
        if not state:
            return False
        stage = state.get("stage", 1)
    low = text.strip().lower()
    if stage == 1:
        if low in ("y", "yes", "j", "ja"):
            _finish_confirm(chat_id, {"allow": True}); return True
        if low in ("a", "always"):
            _finish_confirm(chat_id, {"allow": True, "always": True}); return True
        if low in ("n", "no", "nein"):
            _finish_confirm(chat_id, {"allow": False, "reason": "user denied"}); return True
        if low in ("r", "reason"):
            with _confirm_lock:
                st = _pending_confirms.get(chat_id)
                if st:
                    st["stage"] = 2
            if _bot_app:
                try:
                    await _bot_app.bot.send_message(chat_id=chat_id, text="Grund für die Ablehnung?")
                except Exception:
                    pass
            return True
        if _bot_app:
            try:
                await _bot_app.bot.send_message(chat_id=chat_id, text="Bitte antworte mit y/n/r/a.")
            except Exception:
                pass
        return True
    else:
        _finish_confirm(chat_id, {"allow": False,
                                  "reason": text.strip() or "user denied"})
        return True

def _format_table_as_pre(text: str) -> str:
    def _replace(m):
        table = m.group(0).strip()
        escaped = (table
                   .replace("&", "&amp;")
                   .replace("<", "&lt;")
                   .replace(">", "&gt;"))
        return f"<pre><code>{escaped}</code></pre>"
    return re.sub(
        r"(?:^|\n)((?:\|[^\n]+\|\n?)+)",
        _replace, text)

def markdown_to_html(text: str) -> str:
    if not text:
        return ""

    placeholders: Dict[str, str] = {}
    counter = [0]

    def _protect(pattern, replacer):
        nonlocal text
        def _wrap(m):
            key = f"%%PH{counter[0]}%%"
            counter[0] += 1
            placeholders[key] = replacer(m)
            return key
        text = re.sub(pattern, _wrap, text, flags=re.DOTALL)

    _protect(
        r"```(\w*)\s*\n(.*?)```",
        lambda m: (
            "<pre><code>"
            + (m.group(2)
               .replace("&", "&amp;")
               .replace("<", "&lt;")
               .replace(">", "&gt;"))
            + "</code></pre>"))

    _protect(
        r"`([^`\n]+)`",
        lambda m: (
            "<code>"
            + (m.group(1)
               .replace("&", "&amp;")
               .replace("<", "&lt;")
               .replace(">", "&gt;"))
            + "</code>"))

    text = _format_table_as_pre(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!_)_([^_\n]+?)_(?!_)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"^#{1,3}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)

    for key, html in placeholders.items():
        text = text.replace(key, html)

    return text

def split_markdown_safe(text: str, max_len: int = 4000) -> list:
    chunks = []
    current_chunk = ""
    paragraphs = text.split('\n\n')
    for para in paragraphs:
        if len(para) > max_len:
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = ""
            lines = para.split('\n')
            temp_chunk = ""
            for line in lines:
                if len(temp_chunk) + len(line) + 1 > max_len:
                    if temp_chunk:
                        chunks.append(temp_chunk.strip())
                    temp_chunk = line
                else:
                    temp_chunk += '\n' + line if temp_chunk else line
            if temp_chunk:
                chunks.append(temp_chunk.strip())
            continue
        if len(current_chunk) + len(para) + 2 > max_len:
            if current_chunk:
                chunks.append(current_chunk.strip())
            current_chunk = para
        else:
            current_chunk += '\n\n' + para if current_chunk else para
    if current_chunk.strip():
        chunks.append(current_chunk.strip())
    return chunks if chunks else [text[:max_len]]

def get_agent(chat_id: str) -> AgentCore:
    if chat_id not in agents:
        q = queue.Queue()
        notification_queues[chat_id] = q
        def notifier(msg: str):
            q.put(msg)
        agent = AgentCore(
            session_id=f"tg_{chat_id}",
            enable_tts=False,
            notifier=notifier,
            telegram_mode=True)
        agent.tool_manager.set_confirm_handler(make_confirm_handler(chat_id))
        agent.tool_manager.scheduler = scheduler
        agent.interface = "telegram"
        agents[chat_id] = agent
        print(f"[DEBUG] Agent for {chat_id} created and confirm handler set.")
    return agents[chat_id]


async def split_and_send(update: Update, text: str):
    max_len = 4000
    chunks = split_markdown_safe(text, max_len)
    for chunk in chunks:
        try:
            html_chunk = markdown_to_html(chunk)
            await update.message.reply_text(
                html_chunk,
                parse_mode="HTML",
                disable_web_page_preview=True)
        except Exception as e:
            print(f"[Telegram] HTML formatting failed: {e}")
            await update.message.reply_text(chunk)

async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    agent = get_agent(chat_id)
    agent.start_new_session()
    await update.message.reply_text(f"✨ New session started: {agent.get_session_id()}")

async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    agent = get_agent(chat_id)
    if not context.args:
        sessions = agent.session_manager.list_sessions()
        if sessions:
            sessions_with_time = []
            for s in sessions:
                path = p("data", "sessions", f"{s}.json")
                if os.path.exists(path):
                    mtime = os.path.getmtime(path)
                    mtime_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                else:
                    mtime = 0
                    mtime_str = "unknown"
                sessions_with_time.append((s, mtime, mtime_str))
            sessions_with_time.sort(key=lambda x: x[1])  # oldest -> newest
            current = agent.get_session_id()
            lines = []
            for s, _, ts in sessions_with_time:
                marker = " ← active" if s == current else ""
                lines.append(f"• <code>{s}</code> (accessed: {ts}){marker}")
            text = f"<b>Available sessions ({len(sessions_with_time)}):</b>\n" + "\n".join(lines)
            text += "\n\n<code>/session &lt;id&gt;</code> to switch"
            await update.message.reply_text(text, parse_mode="HTML")
        else:
            await update.message.reply_text("No sessions available.")
        return
    session_id = context.args[0]
    try:
        agent.switch_to(session_id)
        await update.message.reply_text(f"🔄 Switched to session: {session_id}")
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")

async def cmd_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = str(update.effective_chat.id)
        agent = get_agent(chat_id)
        tokens = agent.get_total_tokens()
        has_tiktoken = "tiktoken" if getattr(agent.session_manager, "tokenizer", None) else "fallback"
        await update.message.reply_text(f"🔢 Tokens: {tokens} ({has_tiktoken})")
    except Exception as e:
        await update.message.reply_text(f"❌ Error in /tokens: {type(e).__name__}: {str(e)[:200]}")

async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = str(update.effective_chat.id)
        agent = get_agent(chat_id)
        ctx = agent.context_size
        thr = agent.compression_threshold
        source = "config.json" if agent.config.get("compression_threshold") is not None else "auto (ctx_size - reserve)"
        text = (f"📊 <b>Compression Threshold</b>\n"
                f"Value: <code>{thr}</code>\n"
                f"Context Size: <code>{ctx}</code>\n"
                f"Source: {source}")
        await update.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {type(e).__name__}: {str(e)[:200]}")

async def cmd_ctx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    agent = get_agent(chat_id)
    if not context.args:
        await update.message.reply_text(
            f"📏 Current context size: <code>{agent.context_size}</code>\n"
            f"Usage: <code>/ctx &lt;number&gt;</code>",
            parse_mode="HTML")
        return
    try:
        ctx_value = int(context.args[0])
        if ctx_value < 1024:
            await update.message.reply_text("❌ Context size must be at least 1024.")
            return
    except ValueError:
        await update.message.reply_text("❌ Invalid value. Integer expected.")
        return
    agent.config["context_size"] = ctx_value
    agent._save_config()
    service_path = os.path.expanduser("~/.config/systemd/user/llama-server.service")
    if os.path.exists(service_path):
        try:
            with open(service_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            new_lines = []
            for line in lines:
                if re.search(r'^\s*(?:-c|--ctx-size)(?:\s|=)+(\d+)', line):
                    new_lines.append(re.sub(
                        r'((?:-c|--ctx-size)(?:\s|=)+)(\d+)',
                        lambda m: m.group(1) + str(ctx_value), line))
                else:
                    new_lines.append(line)
            with open(service_path, "w", encoding="utf-8") as f:
                f.write("".join(new_lines))
        except Exception as e:
            await update.message.reply_text(f"⚠️ Service file update: {e}")
    agent.context_size = ctx_value
    agent.compression_threshold = agent._calculate_compression_threshold(ctx_value)
    await update.message.reply_text(f"🔄 Restarting service with ctx-size={ctx_value}...")
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, timeout=30)
        subprocess.run(["systemctl", "--user", "restart", "llama-server"], capture_output=True, text=True, timeout=60)
        r3 = subprocess.run(["systemctl", "--user", "is-active", "llama-server"], capture_output=True, text=True, timeout=10)
        status = r3.stdout.strip()
        if status == "active":
            await update.message.reply_text(f"✅ Context size set: <code>{ctx_value}</code>", parse_mode="HTML")
        else:
            await update.message.reply_text(f"⚠️ Service status: {status}")
    except Exception as e:
        await update.message.reply_text(f"❌ Restart failed: {e}")

async def cmd_listtools(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with open(p("config", "toolpool.txt"), "r", encoding="utf-8") as f:
            pool = json.load(f)
        names = []
        for tool in pool:
            func = tool.get("function", {})
            name = func.get("name", "")
            if name:
                names.append(name)
        if names:
            text = "<b>Available tools:</b>\n" + "\n".join([f"• <code>{n}</code>" for n in names])
        else:
            text = "No tools found in toolpool.txt."
        await update.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Error in /listtools: {type(e).__name__}: {str(e)[:200]}")

async def cmd_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    agent = get_agent(chat_id)
    result = agent.compress_history()
    await update.message.reply_text(result)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """<b>Available commands:</b>

<code>/new</code> – Start a new session
<code>/session &lt;id&gt;</code> – Switch to a session
<code>/clear</code> – Clear current history
<code>/history</code> – Show history
<code>/tokens</code> – Token count (tiktoken)
<code>/threshold</code> – Show compression threshold
<code>/config</code> – Show config
<code>/listtools</code> – List available tools
<code>/offloading [on|off]</code> – Toggle tool offloading
<code>/showThinking [on|off]</code> – Show thinking blocks
<code>/voice [on|off]</code> – Control TTS
<code>/ctx [number]</code> – Set context size (lists without arg)
<code>/zip</code> or <code>/shrink</code> – Compress history
<code>/info</code> – Session info
<code>/personality [name]</code> – Switch personality (lists without arg)
<code>/image &lt;path&gt; [text]</code> – Analyze image (vision)
<code>/help</code> – This help
<code>/sched</code> – Show / create / delete scheduled tasks"""
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = str(update.effective_chat.id)
        agent = get_agent(chat_id)
        session_id = agent.get_session_id()
        try:
            tokens = agent.get_total_tokens()
        except Exception as e:
            tokens = f"Error: {type(e).__name__}"
        try:
            active = [t['function']['name'] for t in agent.tool_manager.get_active_tools()]
        except Exception:
            active = []
        session_path = p("data", "sessions", f"{session_id}.json")
        created_at = "unknown"
        last_access = "unknown"
        if os.path.exists(session_path):
            stat = os.stat(session_path)
            created_at = datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M:%S")
            last_access = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        text = (
            f"<b>Session Info</b>\n"
            f"ID: <code>{session_id}</code>\n"
            f"Tokens: {tokens}\n"
            f"Created: {created_at}\n"
            f"Last access: {last_access}\n"
            f"Active tools: {', '.join(active) if active else 'None'}")
        await update.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        error_msg = f"❌ /info error: {type(e).__name__}: {str(e)[:500]}"
        try:
            await update.message.reply_text(error_msg)
        except Exception:
            print(f"CRITICAL: /info could not reply: {error_msg}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    agent = get_agent(chat_id)
    if not bool(agent.config.get("vision_enabled", True)):
        await update.message.reply_text(
            "❌ Vision is disabled. Set <code>vision_enabled=true</code> in config.json.",
            parse_mode="HTML")
        return
    photo = update.message.photo[-1]
    photo_file = await photo.get_file()
    tmp_dir = tempfile.mkdtemp()
    img_path = os.path.join(tmp_dir, f"photo_{photo.file_id}.jpg")
    await photo_file.download_to_drive(img_path)
    await update.message.reply_text("🖼️ Image received. Analyzing...")
    b64_url = agent._process_image(img_path)
    if b64_url:
        caption = update.message.caption or "Describe this image."
        history = agent.session_manager.load_session(agent.get_session_id())
        history.append({
            "role": "user",
            "content": [
                {"type": "text", "text": caption},
                {"type": "image_url", "image_url": {"url": b64_url}}]})
        agent.session_manager.save_session(agent.get_session_id(), history)
        try:
            response = await asyncio.to_thread(agent.chat, caption, b64_url)
            await split_and_send(update, response)
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)[:200]}")
    else:
        await update.message.reply_text("❌ Image processing failed.")
    try:
        os.remove(img_path)
        os.rmdir(tmp_dir)
    except Exception:
        pass

async def send_tts_voice(update: Update, agent: AgentCore, text: str):
    if not (agent.tts_manager and agent.tts_manager.enabled):
        return
    try:
        tts_wav = tempfile.mkstemp(suffix=".wav")[1]
        await asyncio.to_thread(agent.tts_manager.synthesize_to_file, text, tts_wav)
        if os.path.exists(tts_wav) and os.path.getsize(tts_wav) > 0:
            tts_ogg = tts_wav.replace(".wav", ".ogg")
            conv = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", tts_wav, "-c:a", "libopus",
                "-b:a", "64k", "-application", "audio", tts_ogg,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
            _, stderr = await conv.communicate()
            if conv.returncode == 0 and os.path.exists(tts_ogg):
                with open(tts_ogg, "rb") as vf:
                    await update.message.reply_voice(voice=vf)
                os.remove(tts_ogg)
            else:
                with open(tts_wav, "rb") as vf:
                    await update.message.reply_document(document=vf)
        try:
            os.remove(tts_wav)
        except OSError:
            pass
    except Exception as e:
        print(f"[TTS] Error: {type(e).__name__}: {e}")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    agent = get_agent(chat_id)
    voice = update.message.voice or update.message.audio
    if not voice:
        await update.message.reply_text("❌ No voice message detected.")
        return
    voice_file = await voice.get_file()
    tmp_dir = tempfile.mkdtemp()
    voice_path = os.path.join(tmp_dir, f"voice_{voice.file_id}.ogg")
    wav_path = os.path.join(tmp_dir, f"voice_{voice.file_id}.wav")
    try:
        await voice_file.download_to_drive(voice_path)
    except Exception as e:
        await update.message.reply_text(f"❌ Download failed: {e}")
        return
    await update.message.reply_text("🎙️ Transcribing...")
    try:
        subprocess.run(
            ["ffmpeg", "-i", voice_path, "-ar", "16000", "-ac", "1", "-y", wav_path],
            capture_output=True, timeout=15, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        await update.message.reply_text(f"❌ Audio conversion failed: {e}")
        return
    finally:
        try:
            os.remove(voice_path)
        except Exception:
            pass
    stt_cmd = agent.config.get("stt_command", "")
    transcript = ""
    if not stt_cmd:
        if shutil.which("whisper"):
            stt_cmd = "whisper {audio} --model tiny --language German --output_format txt --output_dir {dir}"
        else:
            await update.message.reply_text("❌ No STT configured. Install whisper or set 'stt_command' in config.json.")
            return
    txt_dir = tempfile.mkdtemp()
    formatted_cmd = stt_cmd.format(audio=wav_path, dir=txt_dir)
    try:
        result = subprocess.run(formatted_cmd, shell=True, capture_output=True, text=True, timeout=60)
        txt_file = os.path.join(txt_dir, os.path.basename(wav_path).replace(".wav", ".txt"))
        if os.path.exists(txt_file):
            with open(txt_file, "r", encoding="utf-8") as f:
                transcript = f.read().strip()
        else:
            transcript = result.stdout.strip()
    except Exception as e:
        await update.message.reply_text(f"❌ STT error: {e}")
        return
    finally:
        try:
            os.remove(wav_path)
            shutil.rmtree(txt_dir, ignore_errors=True)
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
    if not transcript:
        await update.message.reply_text("⚠️ No transcription received.")
        return
    await update.message.reply_text(f"📝 Transcript: {transcript}")
    q = notification_queues.get(chat_id)
    thinking_msg = await update.message.reply_text("🤔 Thinking...")
    stop_event = asyncio.Event()
    sent_ids = set()
    async def flush_loop():
        while not stop_event.is_set():
            got_any = False
            while True:
                try:
                    msg = q.get_nowait()
                    if msg not in sent_ids:
                        sent_ids.add(msg)
                        await update.message.reply_text(msg)
                        got_any = True
                except queue.Empty:
                    break
            if not got_any:
                await asyncio.sleep(0.2)
    flusher = asyncio.create_task(flush_loop())
    response = None
    try:
        response = await asyncio.to_thread(agent.chat, transcript)
    except Exception as e:
        print(f"Agent.chat ERROR (voice): {e}")
        response = f"❌ Internal error: {type(e).__name__}: {str(e)[:200]}"
    finally:
        stop_event.set()
        await asyncio.sleep(0.5)
        try:
            flusher.cancel()
            await flusher
        except asyncio.CancelledError:
            pass
    while True:
        try:
            msg = q.get_nowait()
            if msg not in sent_ids:
                await update.message.reply_text(msg)
        except queue.Empty:
            break
    try:
        await thinking_msg.delete()
    except Exception:
        try:
            await thinking_msg.edit_text("✅")
        except Exception:
            pass
    if response is not None:
        await _send_response_with_images(update, agent, response)
        await send_tts_voice(update, agent, response)
    else:
        await update.message.reply_text("❌ Processing error.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = str(update.effective_chat.id)
    if await handle_confirm_reply(chat_id, text):
        return
    agent = get_agent(chat_id)
    q = notification_queues.get(chat_id)
    thinking_msg = await update.message.reply_text("🤔 Thinking...")
    stop_event = asyncio.Event()
    sent_ids = set()
    async def flush_loop():
        while not stop_event.is_set():
            got_any = False
            while True:
                try:
                    msg = q.get_nowait()
                    if msg not in sent_ids:
                        sent_ids.add(msg)
                        await update.message.reply_text(msg)
                        got_any = True
                except queue.Empty:
                    break
            if not got_any:
                await asyncio.sleep(0.2)
    flusher = asyncio.create_task(flush_loop())
    response = None
    try:
        response = await asyncio.to_thread(agent.chat, text)
    except Exception as e:
        print(f"Agent.chat ERROR: {e}")
        response = f"❌ Internal error: {type(e).__name__}: {str(e)[:200]}"
    finally:
        stop_event.set()
        await asyncio.sleep(0.5)
        try:
            flusher.cancel()
            await flusher
        except asyncio.CancelledError:
            pass
    while True:
        try:
            msg = q.get_nowait()
            if msg not in sent_ids:
                await update.message.reply_text(msg)
        except queue.Empty:
            break
    try:
        await thinking_msg.delete()
    except Exception:
        try:
            await thinking_msg.edit_text("✅")
        except Exception:
            pass
    if response is not None:
        await _send_response_with_images(update, agent, response)
        await send_tts_voice(update, agent, response)
    else:
        await update.message.reply_text("❌ Processing error.")

async def _send_response_with_images(update: Update, agent: AgentCore, response: str):
    try:
        send_image_pattern = re.compile(r'\[SEND_IMAGE:([^\]]+)\]')
        markers_from_response = send_image_pattern.findall(response)
        all_markers = list(dict.fromkeys(markers_from_response))
        history = agent.session_manager.load_session(agent.get_session_id())
        for msg in history:
            if msg.get("role") == "tool":
                try:
                    result = json.loads(msg.get("content", "{}"))
                    marker = result.get("marker", "")
                    if marker and marker.startswith("[SEND_IMAGE:"):
                        img_path = marker[len("[SEND_IMAGE:"):].rstrip("]")
                        if img_path not in all_markers:
                            all_markers.append(img_path)
                except (json.JSONDecodeError, AttributeError):
                    pass
        if all_markers:
            clean_response = send_image_pattern.sub('', response).strip()
            clean_response = re.sub(r'\n{2,}', '\n', clean_response).strip()
            if clean_response:
                await split_and_send(update, clean_response)
            for img_path in all_markers:
                if os.path.exists(img_path):
                    try:
                        await update.message.reply_photo(photo=open(img_path, "rb"))
                    except Exception as e:
                        await update.message.reply_text(f"❌ Failed to send image: {e}")
                else:
                    await update.message.reply_text(f"❌ Image not found: {img_path}")
            history = agent.session_manager.load_session(agent.get_session_id())
            history_modified = False
            for msg in history:
                if msg.get("role") == "tool":
                    try:
                        result = json.loads(msg.get("content", "{}"))
                        marker = result.get("marker", "")
                        if marker and marker.startswith("[SEND_IMAGE:"):
                            result["marker"] = "[SENT]"
                            msg["content"] = json.dumps(result, ensure_ascii=False)
                            history_modified = True
                    except (json.JSONDecodeError, AttributeError):
                        pass
            if history_modified:
                agent.session_manager.save_session(agent.get_session_id(), history)
        else:
            await split_and_send(update, response)
    except Exception as e:
        print(f"Send response ERROR: {e}")
        try:
            await update.message.reply_text(f"❌ Send error: {type(e).__name__}: {str(e)[:200]}")
        except Exception:
            pass

def _tg_files_config():
    try:
        with open(p("config", "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    normalized = {}
    for k, v in cfg.items():
        key = str(k).strip()
        if isinstance(v, str):
            normalized[key] = v.strip()
        else:
            normalized[key] = v
    return normalized

def _sanitize_tg_filename(name):
    name = os.path.basename(str(name or ""))
    name = re.sub(r"[^A-Za-z0-9._\- ]", "_", name)
    name = re.sub(r"\s+", "_", name).strip()
    if not name:
        name = "file"
    return name[:180]

def _unique_tg_path(dir_path, filename):
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(dir_path, filename)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(dir_path, f"{base}_{i}{ext}")
        i += 1
    return candidate

def _masked_config(cfg: Dict) -> Dict:
    masked = dict(cfg)
    for k in ("bot_token", "spotify_client_id", "spotify_client_secret", "api_key"):
        if masked.get(k):
            masked[k] = str(masked[k])[:4] + "…"
    return masked

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    doc = update.message.document
    if not doc:
        await update.message.reply_text("❌ No file detected.")
        return
    cfg = _tg_files_config()
    try:
        max_mb = int(cfg.get("tg_max_file_size_mb", 50))
    except Exception:
        max_mb = 20
    max_bytes = max_mb * 1024 * 1024
    if doc.file_size and doc.file_size > max_bytes:
        await update.message.reply_text(f"❌ File too large. Limit: {max_mb} MB.")
        return
    base_dir = cfg_path(cfg, "tg_files_dir", "data/tg_files")
    target_dir = os.path.join(base_dir, chat_id)
    os.makedirs(target_dir, exist_ok=True)
    raw_name = doc.file_name or f"document_{doc.file_unique_id or 'unknown'}"
    safe_name = _sanitize_tg_filename(raw_name)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = _unique_tg_path(target_dir, f"{ts}_{safe_name}")
    await update.message.reply_text(f"⬇️ Saving file: {doc.file_name or safe_name}")
    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(save_path)
    except Exception as e:
        try:
            if os.path.exists(save_path):
                os.remove(save_path)
        except Exception:
            pass
        await update.message.reply_text(
            f"❌ Download failed: {type(e).__name__}: {str(e)[:200]}")
        return
    size = os.path.getsize(save_path) if os.path.exists(save_path) else 0
    await update.message.reply_text(
        f"✅ File saved:\n<code>{save_path}</code>\nSize: {size} bytes",
        parse_mode="HTML")

async def scheduled_task_loop(application: Application):
    interval = scheduler.check_interval
    try:
        while True:
            await asyncio.sleep(interval)
            if not scheduler.enabled:
                continue
            try:
                scheduler.recover_stale_tasks()
                for task in scheduler.get_due_tasks():
                    task_id = task.get("id")
                    mode = scheduler.get_delivery_mode(task, "telegram")
                    if not mode:
                        continue
                    if not scheduler.claim_task(task_id, "telegram"):
                        continue
                    try:
                        chat_id = task.get("chat_id") or scheduler.default_chat_id
                        if not chat_id:
                            scheduler.mark_failed(task_id, "No chat_id set.")
                            continue
                        agent = get_agent(str(chat_id))
                        response = await asyncio.to_thread(
                            agent.chat, task.get("prompt", ""))
                        html_response = f"⏰ <b>Reminder</b>\n\n{markdown_to_html(response)}"
                        await application.bot.send_message(
                            chat_id=int(chat_id),
                            text=html_response,
                            parse_mode="HTML")
                        scheduler.mark_done(task_id, delivered_via=f"telegram:{mode}")
                        print(f"⏰ Task {task_id} executed via Telegram ({mode}).")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        print(f"⏰ Scheduler error task {task_id}: {e}")
                        scheduler.mark_failed(task_id, str(e))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"⏰ Scheduler loop error: {e}")
    except asyncio.CancelledError:
        print("⏰ Scheduler loop stopped by shutdown.")
        raise

async def post_init(application: Application):
    set_bot_runtime(asyncio.get_running_loop(), application)
    application.bot_data["scheduler_task"] = asyncio.create_task(
        scheduled_task_loop(application))
    print("⏰ Scheduler loop started.")

async def post_shutdown(application: Application):
    task = application.bot_data.get("scheduler_task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    print("⏰ Scheduler task cleanly stopped.")

async def cmd_sched(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if not context.args:
        tasks = scheduler.list_tasks(chat_id=chat_id)
        if not tasks:
            await update.message.reply_text(
                "📭 No scheduled tasks.\n\n"
                "Usage: <code>/sched add YYYY-MM-DD HH:MM:SS &lt;prompt&gt;</code>",
                parse_mode="HTML")
            return
        lines = ["<b>Your scheduled tasks:</b>"]
        for t in tasks:
            status_icon = "🟢" if t.get("status") == "pending" else "⏳"
            lines.append(
                f"{status_icon} <code>{t['id']}</code> | {t['trigger_time']} | "
                f"{t.get('target', 'auto')} | {t['prompt'][:30]}...")
        lines.append("\nUsage: <code>/sched cancel &lt;id&gt;</code>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return
    sub = context.args[0].lower()
    if sub == "add":
        if len(context.args) < 4:
            await update.message.reply_text(
                "❌ Usage: <code>/sched add YYYY-MM-DD HH:MM:SS &lt;prompt&gt;</code>",
                parse_mode="HTML")
            return
        date_str = context.args[1]
        time_str = context.args[2]
        prompt = " ".join(context.args[3:])
        trigger_time = f"{date_str} {time_str}"
        res = scheduler.add_task(
            chat_id=chat_id,
            trigger_time=trigger_time,
            prompt=prompt,
            target="telegram")
        if res.get("success"):
            await update.message.reply_text(f"✅ {res['message']}")
        else:
            await update.message.reply_text(f"❌ {res.get('error')}")
    elif sub == "cancel":
        if len(context.args) < 2:
            await update.message.reply_text("❌ Usage: <code>/sched cancel &lt;id&gt;</code>", parse_mode="HTML")
            return
        task_id = context.args[1]
        res = scheduler.cancel_task(task_id)
        if res.get("success"):
            await update.message.reply_text(f"✅ {res['message']}")
        else:
            await update.message.reply_text(f"❌ {res.get('error')}")
    else:
        await update.message.reply_text("❌ Usage: <code>/sched add ...</code> or <code>/sched cancel &lt;id&gt;</code>", parse_mode="HTML")

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    agent = get_agent(chat_id)
    agent.clear_current()
    await update.message.reply_text("🧹 History cleared. System prompt preserved.")

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    agent = get_agent(chat_id)
    history = agent.session_manager.load_session(agent.get_session_id())
    lines = []
    for m in history:
        role = m['role']
        content = m.get('content', '')
        if role == 'system':
            lines.append(f"🔴 SYSTEM: {content[:80]}...")
        elif role == 'user':
            lines.append(f"🔵 USER: {content[:100]}")
        elif role == 'assistant':
            has_tools = " 🔧" if m.get('tool_calls') else ""
            lines.append(f"🟢 ASSISTANT{has_tools}: {content[:150]}")
        elif role == 'tool':
            lines.append(f"🟡 TOOL: {content[:100]}")
    text = "\n".join(lines) if lines else "History is empty."
    await split_and_send(update, text)

async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    agent = get_agent(chat_id)
    text = json.dumps(_masked_config(agent.config), indent=2, ensure_ascii=False)
    await split_and_send(update, f"⚙️ Config:\n```\n{text}\n```")

async def cmd_offloading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    agent = get_agent(chat_id)
    if not context.args:
        status = "ON" if agent.offloading_enabled else "OFF"
        await update.message.reply_text(f"💾 Offloading: {status}")
        return
    arg = context.args[0].lower()
    if arg in ("on", "off"):
        agent.offloading_enabled = (arg == "on")
        agent.config["offloading_enabled"] = agent.offloading_enabled
        agent._save_config()
        status = "ON" if agent.offloading_enabled else "OFF"
        await update.message.reply_text(f"💾 Offloading: {status}")
    else:
        await update.message.reply_text("❌ Usage: /offloading [on|off]")

async def cmd_showthinking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    agent = get_agent(chat_id)
    if not context.args:
        status = "ON" if agent.show_thinking else "OFF"
        await update.message.reply_text(f"🧠 Thinking: {status}")
        return
    arg = context.args[0].lower()
    if arg in ("on", "off"):
        agent.show_thinking = (arg == "on")
        agent.config["show_thinking"] = agent.show_thinking
        agent._save_config()
        status = "ON" if agent.show_thinking else "OFF"
        await update.message.reply_text(f"🧠 Thinking: {status}")
    else:
        await update.message.reply_text("❌ Usage: /showThinking [on|off]")

async def cmd_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    agent = get_agent(chat_id)
    if not context.args:
        await update.message.reply_text("❌ Usage: /image <path> [optional text]")
        return
    if not bool(agent.config.get("vision_enabled", True)):
        await update.message.reply_text(
            "❌ Vision is disabled. Set <code>vision_enabled=true</code> in config.json.",
            parse_mode="HTML")
        return
    img_path = context.args[0]
    img_text = " ".join(context.args[1:]) if len(context.args) > 1 else "Describe this image."
    b64_url = agent._process_image(img_path)
    if not b64_url:
        await update.message.reply_text(f"❌ Image not found or invalid: {img_path}")
        return
    history = agent.session_manager.load_session(agent.get_session_id())
    history.append({
        "role": "user",
        "content": [
            {"type": "text", "text": img_text},
            {"type": "image_url", "image_url": {"url": b64_url}}]})
    agent.session_manager.save_session(agent.get_session_id(), history)
    thinking_msg = await update.message.reply_text("🤔 Analyzing...")
    try:
        response = await asyncio.to_thread(agent.chat, img_text, b64_url)
        await split_and_send(update, response)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")
    finally:
        try:
            await thinking_msg.delete()
        except Exception:
            pass

async def cmd_personality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    agent = get_agent(chat_id)
    if not context.args:
        personas_dir = p("personas")
        if os.path.exists(personas_dir):
            files = sorted([f.replace(".md", "") for f in os.listdir(personas_dir) if f.endswith(".md")])
            if files:
                text = "<b>Available personalities:</b>\n" + "\n".join([f"• <code>{p}</code>" for p in files])
                text += "\n\nUsage: <code>/personality &lt;name&gt;</code>"
            else:
                text = "No personalities found in personas/."
        else:
            text = "❌ personas/ directory not found."
        await update.message.reply_text(text, parse_mode="HTML")
        return
    name = context.args[0].lower()
    result = agent.set_personality(name)
    if result.get("success"):
        await update.message.reply_text(f"✅ {result['message']}")
    else:
        await update.message.reply_text(f"❌ {result['error']}")

async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    agent = get_agent(chat_id)
    if not context.args:
        status = "ON" if (agent.tts_manager and agent.tts_manager.enabled) else "OFF"
        await update.message.reply_text(f"🔊 TTS: {status}")
        return
    arg = context.args[0].lower()
    if arg in ("on", "off"):
        if not agent.tts_manager:
            await update.message.reply_text("❌ TTS not available.")
            return
        agent.tts_manager.toggle(arg == "on")
        status = "ON" if agent.tts_manager.enabled else "OFF"
        await update.message.reply_text(f"🔊 TTS: {status}")
    else:
        await update.message.reply_text("❌ Usage: /voice [on|off]")

async def auth_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    allowed = context.application.bot_data.get("authorized_chat")
    if not allowed or update.effective_chat is None:
        return
    if str(update.effective_chat.id) != str(allowed):
        raise ApplicationHandlerStop

async def _dbg_callback(update, context):
    if update.callback_query:
        print(f"[DBG] Callback angekommen: {update.callback_query.data!r}")
    return False

def main():
    config_path = p("config", "config.json")
    if not os.path.exists(config_path):
        print(config_path, "not found!")
        return
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    token = config.get("bot_token", "")
    if not token or token == "YOUR_BOT_TOKEN_HERE":
        print("Please set bot_token in config.json!")
        return

    application = (
    Application.builder()
    .token(token)
    .concurrent_updates(True)
    .post_init(post_init)
    .post_shutdown(post_shutdown)
    .build())
    application.bot_data["authorized_chat"] = str(config.get("chat_id", "") or "")
    application.add_handler(TypeHandler(Update, _dbg_callback), group=-2)
    application.add_handler(TypeHandler(Update, auth_filter), group=-1)
    application.add_handler(CallbackQueryHandler(handle_confirm_callback, pattern=r"^toolconfirm\|"), group=0)
    application.add_handler(CommandHandler("new", cmd_new))
    application.add_handler(CommandHandler("session", cmd_session))
    application.add_handler(CommandHandler("clear", cmd_clear))
    application.add_handler(CommandHandler("history", cmd_history))
    application.add_handler(CommandHandler("tokens", cmd_tokens))
    application.add_handler(CommandHandler("config", cmd_config))
    application.add_handler(CommandHandler("listtools", cmd_listtools))
    application.add_handler(CommandHandler("offloading", cmd_offloading))
    application.add_handler(CommandHandler("showthinking", cmd_showthinking))
    application.add_handler(CommandHandler("zip", cmd_zip))
    application.add_handler(CommandHandler("shrink", cmd_zip))
    application.add_handler(CommandHandler("image", cmd_image))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("info", cmd_info))
    application.add_handler(CommandHandler("threshold", cmd_threshold))
    application.add_handler(CommandHandler("ctx", cmd_ctx))
    application.add_handler(CommandHandler("personality", cmd_personality))
    application.add_handler(CommandHandler("voice", cmd_voice))
    application.add_handler(CommandHandler("sched", cmd_sched))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Bot started (with scheduler)!")
    application.run_polling(
        allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY,],)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye 👋🏻")
