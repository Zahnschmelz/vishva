import os
import json
import uuid
import requests
import time
from typing import Dict, List, Any, Optional, Set
from .paths import p, cfg_path
try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False

class SessionManager:
    def __init__(self, config_path: str = None):
        self.config_path = config_path or p("config", "config.json")
        self.config = self._load_config()
        self.keep_n = self.config.get("history_keep_n", 5)
        self.tool_ttl_turns = self.config.get("tool_ttl_turns", 4)
        self.base_url = self.config.get("base_url", "http://localhost:11434/v1")
        self.model = self.config.get("model", "llama3.1")
        self.api_key = self.config.get("api_key", "ollama")
        self.token_counting = str(self.config.get("token_counting", "tiktoken"))
        os.makedirs(p("data", "sessions"), exist_ok=True)
        os.makedirs(p("data", "agent_cache"), exist_ok=True)
        if TIKTOKEN_AVAILABLE:
            try:
                self.tokenizer = tiktoken.encoding_for_model("gpt-4")
            except Exception:
                self.tokenizer = tiktoken.get_encoding("cl100k_base")
        else:
            self.tokenizer = None

    def _debug_level(self) -> int:
        v = self.config.get("debug", 0)
        if isinstance(v, bool):
            return 2 if v else 0
        try:
            return max(0, min(2, int(v)))
        except (TypeError, ValueError):
            return 0

    def _load_config(self) -> Dict[str, Any]:
        if os.path.exists(self.config_path):
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _cache_dir(self, session_id: str) -> str:
        return p("data", "agent_cache", session_id)

    def _cache_file(self, session_id: str, tc_id: str) -> str:
        return f"{self._cache_dir(session_id)}/{tc_id}.json"

    def create_session(self, system_prompt: str) -> str:
        session_id = str(uuid.uuid4())[:8]
        history = [{"role": "system", "content": system_prompt}]
        self.save_session(session_id, history)
        return session_id

    def load_session(self, session_id: str) -> List[Dict[str, Any]]:
        path = p("data", "sessions", f"{session_id}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return []

    def save_session(self, session_id: str, history: List[Dict[str, Any]]):
        path = p("data", "sessions", f"{session_id}.json")

        with open(path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)

    def list_sessions(self) -> List[str]:
        if not os.path.exists(p("data", "sessions")):
            return []
        files = [f.replace(".json", "") for f in os.listdir(p("data", "sessions")) if f.endswith(".json")]
        return sorted(files)

    def get_latest_session(self) -> Optional[str]:
        sessions = self.list_sessions()
        if not sessions:
            return None
        latest = max(sessions, key=lambda s: os.path.getmtime(p("data", "sessions", f"{s}.json")))
        return latest

    def clear_session(self, session_id: str, system_prompt: str):
        history = [{"role": "system", "content": system_prompt}]
        self.save_session(session_id, history)
        cache_dir = self._cache_dir(session_id)
        if os.path.exists(cache_dir):
            for f in os.listdir(cache_dir):
                os.remove(os.path.join(cache_dir, f))
            os.rmdir(cache_dir)

    def cleanup_empty_sessions(self, protect_session_id: Optional[str] = None) -> Dict[str, Any]:
        sessions_dir = p("data", "sessions")
        if not os.path.exists(sessions_dir):
            return {"removed": 0, "sessions": []}
        files = []
        for f in os.listdir(sessions_dir):
            if not f.endswith(".json"):
                continue
            path = os.path.join(sessions_dir, f)
            try:
                files.append((os.path.getmtime(path), f[:-5], path))
            except Exception:
                continue
        if not files:
            return {"removed": 0, "sessions": []}
        files.sort(reverse=True)
        newest_id = files[0][1]
        removed = []
        now = time.time()
        for mtime, sid, path in files:
            if sid == newest_id:
                continue
            if protect_session_id and sid == str(protect_session_id):
                continue
            if now - mtime < 600:
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                continue
            if not isinstance(history, list):
                continue
            has_content = any(
                isinstance(m, dict) and m.get("role") in ("user", "assistant", "tool")
                for m in history)
            if not has_content:
                try:
                    os.remove(path)
                    removed.append(sid)
                except Exception:
                    pass
        try:
            self._cleanup_orphaned_folders()
        except Exception:
            pass
        return {"removed": len(removed), "sessions": removed}

    def _count_tokens(self, text: str) -> int:
        if self.token_counting == "server" and self.base_url:
            root = self.base_url.rstrip("/")
            if root.endswith("/v1"):
                root = root[:-3]
            try:
                resp = requests.post(
                    f"{root}/tokenize",
                    json={"content": text, "add_special": True},
                    timeout=15)
                if resp.status_code == 200:
                    tokens = resp.json().get("tokens")
                    if isinstance(tokens, list):
                        return len(tokens)
            except Exception:
                pass
            if not getattr(self, "_tokenize_warned", False):
                print("[SessionManager] /tokenize not available—use the tiktoken estimate")
                self._tokenize_warned = True
        if self.tokenizer:
            return len(self.tokenizer.encode(text))
        return len(text.split())

    def get_total_tokens(self, session_id: str, full_system_prompt: str = "",
                         tools: List[Dict] = None, override_history: List[Dict] = None) -> int:
        history = override_history if override_history is not None else self.load_session(session_id)
        if not history:
            return 0
        pieces: List[str] = []
        image_count = 0
        msg_count = 0
        for msg in history:
            msg_count += 1
            content = msg.get("content", "")
            if isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        t = item.get("text", "")
                        if t:
                            pieces.append(t)
                    elif item.get("type") == "image_url":
                        image_count += 1
            elif isinstance(content, str) and content:
                pieces.append(content)
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                func = tc.get("function", {})
                args = func.get("arguments", "")
                if args:
                    pieces.append(args)
                name = func.get("name", "")
                if name:
                    pieces.append(name)
            if msg.get("role") == "tool":
                name = msg.get("name", "")
                if name:
                    pieces.append(name)

        has_system_in_history = history[0].get("role") == "system"
        if full_system_prompt and not has_system_in_history:
            pieces.append(full_system_prompt)
        if tools:
            pieces.append(json.dumps(tools, ensure_ascii=False))

        if self.token_counting == "server":
            total = self._count_tokens("\n".join(pieces))
            total += 4 * msg_count
        elif self.tokenizer:
            total = sum(len(self.tokenizer.encode(p)) for p in pieces)
        else:
            total = sum(len(p.split()) for p in pieces)
        total += 255 * image_count
        return total

    def compress_history(self, session_id: str, rag_offload: bool = False) -> Optional[Dict[str, Any]]:
        history = self.load_session(session_id)
        history_len = len(history)
        if not history or history_len <= self.keep_n + 1:
            self._cleanup_session_cache(session_id, history)
            return None
        system_msg = history[0]
        cut = max(1, len(history) - self.keep_n)
        while cut > 1 and history[cut].get("role") != "user":
            cut -= 1
        recent_msgs = history[cut:]
        middle_msgs = history[1:cut]
        if not middle_msgs:
            self._cleanup_session_cache(session_id, history)
            return None

        if rag_offload:
            offloaded_turns = sum(1 for m in middle_msgs if m.get("role") == "user")
            new_history = [system_msg]
            new_history.append({
                "role": "assistant",
                "content": "[CONTEXT_OFFLOADED] Older parts of this conversation have been moved to RAG's "
                            "long-term memory. Relevant content automatically appears as "
                            "[RAG_CONTEXT]; it can be retrieved specifically via rag_search."})
            new_history.extend(recent_msgs)
            self.save_session(session_id, new_history)
            self._cleanup_session_cache(session_id, new_history)
            self._cleanup_orphaned_folders()
            return {"old_len": history_len, "new_len": len(new_history),
                    "mode": "rag_offload", "offloaded_turns": offloaded_turns}

        summary = self._summarize(middle_msgs)
        if not summary:
            self._cleanup_session_cache(session_id, history)
            return None
        new_history = [system_msg]
        new_history.append({
            "role": "assistant",
            "content": f"[CONTEXT_SUMMARY] {summary}"})
        new_history.extend(recent_msgs)
        self.save_session(session_id, new_history)
        self._cleanup_session_cache(session_id, new_history)
        self._cleanup_orphaned_folders()
        return {"old_len": history_len, "new_len": len(new_history), "summary": summary}

    def _sanitize_for_summary(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        sanitized = []
        for msg in messages:
            new_msg = dict(msg)
            content = msg.get("content")
            if isinstance(content, list):
                new_content = []
                for item in content:
                    if item.get("type") == "image_url":
                        new_content.append({"type": "text", "text": "[IMAGE]"})
                    else:
                        new_content.append(item)
                new_msg["content"] = new_content
            elif isinstance(content, str) and len(content) > 800:
                new_msg["content"] = content[:800] + " …[gekürzt für Summary]"
            sanitized.append(new_msg)
        return sanitized

    def _meta_char_budget(self, max_out_tokens: int) -> int:
        try:
            meta_ctx = int(self.config.get("meta_context_size", 4096) or 4096)
        except (TypeError, ValueError):
            meta_ctx = 4096
        instruction_tokens = 200
        safety = int(meta_ctx * 0.1)
        input_tokens = max(500, meta_ctx - max_out_tokens - instruction_tokens - safety)
        return int(input_tokens * 3)

    def _summarize(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        try:
            max_out = int(self.config.get("meta_summary_max_tokens", 500) or 500)
        except (TypeError, ValueError):
            max_out = 500
        budget = self._meta_char_budget(max_out)

        meta_base = str(self.config.get("meta_model_url", "") or "")
        meta_model = str(self.config.get("meta_model_name", "") or "")
        meta_key = self.config.get("meta_api_key", "") or self.api_key
        meta_timeout = int(self.config.get("meta_model_timeout", 60))

        system_text = (
            "Compression Wizard: Summarize the conversation into a concise, "
            "token-efficient summary. Be sure to INCLUDE:\n"
            "- All file paths and directories\n"
            "- Tools run and their results (success/error)\n"
            "- Code changes (edit_file, write_file) with filenames\n"
            "- Decisions made and plans\n"
            "- Error messages and their causes\n"
            "- Memory keys and important facts\n"
            "Format: Bullet points, no clichés. Do not repeat system prompts.")
        merge_text = (
            "Merge these partial summaries of ONE conversation into a single "
            "concise, token-efficient summary. Keep file paths, tool results, "
            "decisions and errors. Format: bullet points.")

        def meta_call(user_content: str,
                      sys_text: str = system_text,
                      part_label: str = "") -> Optional[str]:
            content = sys_text + (f" (Part {part_label})" if part_label else "")
            return self._summarize_request(
                base=meta_base,
                model=meta_model,
                api_key=meta_key,
                messages=[
                    {"role": "system", "content": content},
                    {"role": "user", "content": user_content}],
                max_tokens=max_out,
                timeout=meta_timeout,
                extra={"chat_template_kwargs": {"enable_thinking": False}},)

        sanitized = self._sanitize_for_summary(messages)
        serialized = [json.dumps(m, ensure_ascii=False) for m in sanitized]
        chunks: List[List[str]] = []
        current: List[str] = []
        size = 0
        for s in serialized:
            if len(s) > budget:
                s = s[:budget]
            cost = len(s) + 2
            if size + cost > budget and current:
                chunks.append(current)
                current, size = [], 0
            current.append(s)
            size += cost
        if current:
            chunks.append(current)

        partials: List[str] = []
        if len(chunks) == 1:
            summary = meta_call("[" + ",".join(chunks[0]) + "]")
            if summary:
                return summary
        else:
            for i, chunk in enumerate(chunks, 1):
                part = meta_call("[" + ",".join(chunk) + "]",
                                 part_label=f"{i}/{len(chunks)}")
                if part:
                    partials.append(part)

        if not partials:
            return self._summarize_request(
                base=self.base_url,
                model=self.model,
                api_key=self.api_key,
                messages=[
                    {"role": "system", "content": system_text},
                    {"role": "user",
                     "content": json.dumps(sanitized, ensure_ascii=False)}],
                max_tokens=800,
                timeout=300,)

        if len(partials) == 1:
            return partials[0]

        while len(partials) > 1:
            merged = "\n".join(f"- {p}" for p in partials)
            if len(merged) <= budget:
                return meta_call(merged, sys_text=merge_text) or partials[0]
            new_partials: List[str] = []
            window: List[str] = []
            size = 0
            for p in partials:
                cost = len(p) + 3
                if size + cost > budget and window:
                    m = meta_call("\n".join(f"- {x}" for x in window),
                                  sys_text=merge_text)
                    new_partials.append(m or window[0])
                    window, size = [], 0
                window.append(p)
                size += cost
            if window:
                m = meta_call("\n".join(f"- {x}" for x in window),
                              sys_text=merge_text)
                new_partials.append(m or window[0])
            if len(new_partials) >= len(partials):
                return partials[0]
            partials = new_partials
        return partials[0]

    def _summarize_request(self, base: str, model: str, api_key: str, messages: List[Dict[str, Any]], max_tokens: int, timeout: int, extra: Optional[Dict[str, Any]] = None) -> Optional[str]:
        if not base or not model:
            return None
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": 0.2,}
        if extra:
            payload.update(extra)
        try:
            response = requests.post(
                f"{base.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout)
            if response.status_code != 200:
                return None
            data = response.json()
            choices = data.get("choices", [])
            if not choices:
                return None
            message = choices[0].get("message", {})
            summary = (message.get("content", "") or message.get("reasoning", "")
                       or message.get("text", ""))
            if not summary or not summary.strip():
                return None
            return summary.strip()
        except Exception:
            return None

    def _load_cache_entry(self, session_id: str, tc_id: str) -> Dict[str, str]:
        path = self._cache_file(session_id, tc_id)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_cache_entry(self, session_id: str, tc_id: str, data: Dict[str, str]):
        cache_dir = self._cache_dir(session_id)
        os.makedirs(cache_dir, exist_ok=True)
        path = self._cache_file(session_id, tc_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def save_chunked_cache(self, session_id: str, tc_id: str, entry: Dict[str, Any], chunk_size: int = 2500) -> int:
        result = entry.get("result", "") or ""
        n_chunks = 0
        if chunk_size and len(result) > chunk_size:
            pieces, rest = [], result
            while rest:
                if len(rest) <= chunk_size:
                    cut = len(rest)
                else:
                    cut = rest.rfind("\n", 0, chunk_size)
                    if cut < chunk_size // 2:
                        cut = chunk_size
                pieces.append(rest[:cut])
                rest = rest[cut:]
            n_chunks = len(pieces)
            for i, text in enumerate(pieces):
                self._save_cache_entry(session_id, f"{tc_id}__c{i}", {
                    "type": "chunk",
                    "name": entry.get("name", ""),
                    "chunk": i,
                    "total": n_chunks,
                    "result": text})
            entry["chunks"] = n_chunks
        self._save_cache_entry(session_id, tc_id, entry)
        return n_chunks

    def _extract_referenced_ids(self, history: List[Dict[str, Any]]) -> Set[str]:
        referenced = set()
        for msg in history:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id", "")
                    if tc_id:
                        referenced.add(tc_id)
            elif msg.get("role") == "tool":
                content = msg.get("content", "")
                tc_id = msg.get("tool_call_id", "")
                if tc_id:
                    referenced.add(tc_id)
                if content.startswith("[POINTER:"):
                    try:
                        ref_id = content.split("/")[-1].replace(".json]", "")
                        if ref_id:
                            referenced.add(ref_id)
                    except Exception:
                        pass
        return referenced

    def _cleanup_session_cache(self, session_id: str, history: List[Dict[str, Any]]):
        cache_dir = self._cache_dir(session_id)
        if not os.path.exists(cache_dir):
            return
        referenced = self._extract_referenced_ids(history)
        removed = 0
        for filename in os.listdir(cache_dir):
            if not filename.endswith(".json"):
                continue
            stem = filename[:-len(".json")]
            tc_id = stem.split("__c")[0] if "__c" in stem else stem
            if tc_id not in referenced:
                try:
                    os.remove(os.path.join(cache_dir, filename))
                    removed += 1
                except Exception:
                    pass

    def _cleanup_orphaned_folders(self):
        if not os.path.exists(p("data", "agent_cache")):
            return
        sessions = set(self.list_sessions())
        removed = 0
        for folder in os.listdir(p("data", "agent_cache")):
            folder_path = os.path.join(p("data", "agent_cache"), folder)
            if not os.path.isdir(folder_path):
                continue
            if folder not in sessions:
                try:
                    for f in os.listdir(folder_path):
                        os.remove(os.path.join(folder_path, f))
                    os.rmdir(folder_path)
                    removed += 1
                except Exception:
                    pass

    def apply_tool_ttl(self, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not history or len(history) <= 2:
            return history
        system_msg = history[0] if history[0].get("role") == "system" else None
        search_start = 1 if system_msg else 0
        turn_indices = [i for i, msg in enumerate(history) if msg.get("role") == "user" and i >= search_start]
        turn_indices.append(len(history))
        new_history = []
        if system_msg:
            new_history.append(system_msg)
        first_turn_start = turn_indices[0] if turn_indices else len(history)
        new_history.extend(history[search_start:first_turn_start])
        for turn_idx in range(len(turn_indices) - 1):
            start = turn_indices[turn_idx]
            end = turn_indices[turn_idx + 1]
            turn_msgs = history[start:end]
            turns_back = (len(turn_indices) - 1) - turn_idx - 1
            if turns_back >= self.tool_ttl_turns:
                turn_msgs = self._compress_turn_tool_pairs(turn_msgs)
            new_history.extend(turn_msgs)
        return new_history

    def _compress_turn_tool_pairs(self, turn_msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = []
        i = 0
        while i < len(turn_msgs):
            msg = turn_msgs[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                tool_calls = msg["tool_calls"]
                tc_ids = {tc.get("id") for tc in tool_calls}
                tool_results = []
                j = i + 1
                while j < len(turn_msgs) and turn_msgs[j].get("role") == "tool":
                    if turn_msgs[j].get("tool_call_id") in tc_ids:
                        tool_results.append(turn_msgs[j])
                        j += 1
                    else:
                        break
                summary_parts = []
                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "unknown")
                    tc_id = tc.get("id", "unknown")
                    result_msg = next(
                        (tr for tr in tool_results if tr.get("tool_call_id") == tc_id),
                        None)
                    status = "done"
                    if result_msg:
                        content = result_msg.get("content", "")
                        try:
                            parsed = json.loads(content)
                            if isinstance(parsed, dict):
                                if parsed.get("error"):
                                    status = "error"
                                elif parsed.get("success") is False:
                                    status = "error"
                                elif isinstance(parsed.get("errors"), list) and len(parsed["errors"]) > 0:
                                    status = "error"
                        except (json.JSONDecodeError, TypeError, ValueError):
                            if "error" in content.lower():
                                status = "error"
                    summary_parts.append(f"{name}[{tc_id[:6]}]={status}")
                cache_hint = ""
                if summary_parts:
                    ids = [tc.get("id", "unknown")[:6] for tc in tool_calls]
                    cache_hint = f" Cache IDs: {', '.join(ids)}"
                summary = f"[TOOL_HISTORY: {' | '.join(summary_parts)}.{cache_hint}]"
                result.append({
                    "role": "assistant",
                    "content": summary})
                i = j
            else:
                result.append(msg)
                i += 1
        return result

    def compress_history_pointers(self, session_id: str, history: List[Dict[str, Any]], offload: bool = True) -> List[Dict[str, Any]]:
        MAX_RESULT = self.config.get("max_tool_result_length", 500)
        MAX_ARGS = self.config.get("max_tool_args_length", 2000)
        new_history = []
        offload_count = 0
        for msg in history:
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                new_msg = {"role": "assistant", "content": msg.get("content", "")}
                new_tool_calls = []
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id", "unknown")
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    arguments = func.get("arguments", "")
                    if len(arguments) > MAX_ARGS:
                        if offload:
                            entry = self._load_cache_entry(session_id, tc_id)
                            if not entry:
                                entry = {}
                            entry.update({"name": name, "arguments": arguments})
                            self._save_cache_entry(session_id, tc_id, entry)
                        truncated = arguments[:MAX_ARGS] + (
                            f"\n[... truncated, full arguments in cache: "
                            f"path={self._cache_file(session_id, tc_id)}]")
                        new_tc = {
                            "id": tc_id,
                            "type": tc.get("type", "function"),
                            "function": {
                                "name": name,
                                "arguments": truncated}}
                    else:
                        new_tc = tc
                    new_tool_calls.append(new_tc)
                new_msg["tool_calls"] = new_tool_calls
                new_history.append(new_msg)
            elif role == "tool":
                tc_id = msg.get("tool_call_id", "unknown")
                result = msg.get("content", "")
                name = msg.get("name", "")
                if offload:
                    entry = self._load_cache_entry(session_id, tc_id)
                    if not entry:
                        entry = {}
                    is_new = not entry.get("result")
                    if len(result) > len(entry.get("result", "")):
                        entry["result"] = result
                    if name and not entry.get("name"):
                        entry["name"] = name
                    self._save_cache_entry(session_id, tc_id, entry)
                    if is_new:
                        offload_count += 1
                if len(result) > MAX_RESULT:
                    short = result[:MAX_RESULT] + f"\n[... truncated, use read_cache with path={self._cache_file(session_id, tc_id)} for full output]"
                else:
                    short = result
                new_history.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": short})
            else:
                new_history.append(msg)
        if offload:
            self._cleanup_session_cache(session_id, new_history)
            if offload_count:
                dbg = self._debug_level()
                if dbg == 1:
                    print("💾")
                elif dbg == 2:
                    print(f"[DEBUG 💾] Offloading: {offload_count} Tool-Result(s) new to ", p("data", "agent_cache", session_id))
        return new_history
