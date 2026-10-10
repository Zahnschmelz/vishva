"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
#from ..paths import p, cfg_path, BASE_DIR
from ..paths import p
#import re
import json
import time
#import shutil
#import subprocess
#from typing import Any, Dict, List, Optional, Tuple
from typing import Any, Dict, List

try:
    import requests
except ImportError:
    requests = None

def _subagent(self, args: Dict[str, Any]) -> Dict[str, Any]:
    task = args.get("task", "")
    if not task:
        return {"error": "task required"}
    try:
        max_turns = int(args.get("max_turns", 15) or 15)
    except (TypeError, ValueError):
        max_turns = 15
    max_turns = max(1, min(max_turns, int(self.config.get("subagent_max_turns_limit", 30))))
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ts = now.replace(":", "").replace(" ", "_")
    sop_hint = ""
    manual_path = p("knowledge", "SOP_COMPLEX_TASKS.md")
    if os.path.exists(manual_path):
        try:
            with open(manual_path, "r", encoding="utf-8") as mf:
                sop_hint = f"\nSOP: {mf.read()[:500].replace(chr(10), ' ')}"
        except Exception:
            pass
    system_prompt = (
        "[SOUL]\nSharp, direct, minimal. No filler. No explanations.\n"
        "[ENGINE]\n"
        f"File/edit/bash task -> script in ./task_{ts}/.{sop_hint}\n"
        f"[Date/Time: {now}]\n"
        "[INSTRUCTION]\n"
        f"Execute: {task}\n"
        "Return compact JSON: "
        '{"status":"success|error","result":"...","files":[],"errors":[]}\n'
        "Max 500 chars in result. No filler. Just JSON.")
    cfg_forbidden = set(self.config.get("subagent_forbidden", []))
    default_forbidden = {
        "speak", "rag_save", "send_image",
        "send_file", "sched_task", "ls_tasks", "cancel_task", "subagent"}
    forbidden = cfg_forbidden | default_forbidden
    subagent_tools = [
        t for t in self.available_tools
        if t.get("function", {}).get("name", "") not in forbidden]
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},]
    base_url = self.config.get("base_url", "http://127.0.0.1:8080/v1")
    api_key = self.config.get("api_key", "llama")
    model = self.config.get("model", "llama3.1")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",}
    ctx_size = int(self.config.get("context_size", 16384))
    reserve = int(self.config.get("loop_rescue_reserve", 4000))
    max_rescue = int(self.config.get("loop_rescue_max_attempts", 2))
    rescue_attempts = 0
    agent_ref = getattr(self, "agent_ref", None)

    def _extract_trace(msgs: List[Dict[str, Any]]) -> str:
        if agent_ref is not None:
            try:
                return agent_ref._extract_tool_trace(msgs)
            except Exception:
                pass
        return "(trace unavailable)"

    def _last_pairs(msgs: List[Dict[str, Any]], keep: int) -> List[Dict[str, Any]]:
        if agent_ref is not None:
            try:
                return agent_ref._last_tool_pairs(msgs, keep)
            except Exception:
                pass
        return []
    turn = 0
    while turn < max_turns:
        est_tokens = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 4
        if (est_tokens > ctx_size - reserve
                and rescue_attempts < max_rescue
                and len(messages) > 4):
            rescue_attempts += 1
            trace = _extract_trace(messages)
            messages = (
                messages[:2]
                + [{
                    "role": "system",
                    "content": (
                        f"[PROGRESS] Subagent-Zwischenstand "
                        f"(Rettung #{rescue_attempts}):\n{trace}\n"
                        "Arbeite ab hier weiter, wiederhole nichts.")}]
                + _last_pairs(messages, 1))
            continue
        turn += 1
        call_messages = [dict(m) for m in messages]
        if self.config.get("budget_hint_enabled", True) and call_messages:
            left = max_turns - turn + 1
            hint = (
                f"\n[BUDGET] Subagent-Runden: {left}/{max_turns} übrig. "
                "Bei <3: abschließen statt neu beginnen.")
            last_c = call_messages[-1].get("content")
            if isinstance(last_c, str):
                call_messages[-1]["content"] = last_c + hint
        payload = {
            "model": model,
            "messages": call_messages,
            "tools": subagent_tools,
            "tool_choice": "auto",
            "stream": False,}
        for key in ("temperature", "top_p", "top_k", "seed"):
            if self.config.get(key) is not None:
                payload[key] = self.config[key]
        import requests
        sub_retry_delay = 1.5
        data = None
        last_err = None
        for sub_attempt in range(2):
            try:
                resp = requests.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=500,)
                resp.raise_for_status()
                data = resp.json()
                last_err = None
                break
            except Exception as e:
                last_err = e
                if sub_attempt == 0:
                    if self.config.get("debug", 0) >= 1:
                        print(f"[Subagent] ⚠️ API-Call fehlgeschlagen (attempt 1/2), retry in {sub_retry_delay}s: {e}")
                    time.sleep(sub_retry_delay)
                    continue
        if data is None:
            return {
                "status": "error",
                "result": f"API error: {last_err}",
                "files": [],
                "errors": [str(last_err)],}
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        content = msg.get("content", "") or ""
        tool_calls = msg.get("tool_calls", [])
        assistant_entry = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_entry["tool_calls"] = tool_calls
        messages.append(assistant_entry)
        if not tool_calls:
            try:
                result = json.loads(content)
                if isinstance(result, dict):
                    return {
                        "success": True,
                        "turns": turn,
                        **result,}
            except json.JSONDecodeError:
                pass
            return {
                "success": True,
                "turns": turn,
                "status": "success",
                "result": content[:500],
                "files": [],
                "errors": [],}
        for tc in tool_calls:
            if tc.get("type") != "function":
                continue
            func = tc.get("function", {})
            name = func.get("name", "")
            tc_id = tc.get("id", "unknown")
            try:
                args_parsed = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                args_parsed = {}
            result = self.execute_tool(name, args_parsed)
            res_str = json.dumps(result, ensure_ascii=False, default=str)
            MAX_RES = int(self.config.get("max_tool_result_length", 3000))
            if len(res_str) > MAX_RES:
                hint_txt = "\n[... truncated]"
                if (self.config.get("offloading_enabled", True)
                        and agent_ref is not None):
                    try:
                        sm = agent_ref.session_manager
                        sid = agent_ref.session_id
                        entry = {
                            "name": name,
                            "arguments": json.dumps(args_parsed, ensure_ascii=False),
                            "result": res_str,}
                        n_chunks = sm.save_chunked_cache(
                            sid, tc_id, entry,
                            chunk_size=int(self.config.get("read_cache_chunk_size", 2500)),)
                        cache_path = sm._cache_file(sid, tc_id)
                        if n_chunks:
                            hint_txt = (
                                f"\n[... truncated ({n_chunks} chunks), "
                                f"use read_cache with path={cache_path} "
                                f"and chunk=0..{n_chunks - 1}]")
                        else:
                            hint_txt = (
                                f"\n[... truncated, use read_cache "
                                f"with path={cache_path} for full output]")
                    except Exception:
                        pass
                res_str = res_str[:MAX_RES] + hint_txt
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "name": name,
                "content": res_str,})
    if not self.config.get("loop_rescue_graceful_finish", True):
        return {
            "success": True,
            "turns": max_turns,
            "status": "error",
            "result": "Max turns reached",
            "files": [],
            "errors": ["max_turns_exceeded"],}
    fin_messages = list(messages) + [{
        "role": "system",
        "content": (
            f"[BUDGET] Subagent-Runden-Limit erreicht ({max_turns}/{max_turns}). "
            "Du hast KEINE Tools mehr. Antworte NUR mit dem kompakten "
            "JSON-Resultat (status/result/files/errors) aus dem bisherigen "
            "Fortschritt. Markiere Unfertiges in errors."),}]
    import requests
    sub_retry_delay = 1.5
    fin_content = None
    for sub_attempt in range(2):
        try:
            fin_payload = {
                "model": model,
                "messages": fin_messages,
                "stream": False,
                "max_tokens": 800,}
            for key in ("temperature", "top_p", "top_k", "seed"):
                if self.config.get(key) is not None:
                    fin_payload[key] = self.config[key]
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=fin_payload,
                timeout=500,)
            resp.raise_for_status()
            fin_content = (resp.json()
                        .get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "") or "")
            break
        except Exception as e:
            if sub_attempt == 0:
                if self.config.get("debug", 0) > 1:
                    print(f"[Subagent] ⚠️ Graceful-finish API-Call fehlgeschlagen (attempt 1/2), retry: {e}")
                time.sleep(sub_retry_delay)
                continue
    if fin_content is not None:
        content = fin_content
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return {
                    "success": True,
                    "turns": max_turns,
                    "partial": True,
                    **parsed,}
        except json.JSONDecodeError:
            pass
        if content.strip():
            return {
                "success": True,
                "turns": max_turns,
                "partial": True,
                "status": "partial",
                "result": content[:500],
                "files": [],
                "errors": ["max_turns_graceful"],}
    return {
        "success": True,
        "turns": max_turns,
        "status": "error",
        "result": "Max turns reached (graceful finish failed)",
        "files": [],
        "errors": ["max_turns_exceeded"],}
