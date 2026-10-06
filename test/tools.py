#!/usr/bin/env python3

import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vishva.test_tool import load_config
from vishva.tool_manager import ToolManager

SCRATCH = os.path.join("tmp", "smoke_test.txt")
SCHED_FLOW = {"sched_task", "ls_tasks", "cancel_task"}
DANGEROUS = {"sys_update", "sys_clean", "turnoff_screen"}
FIRST_RUN_ONLY = {"rag_essential"}


CASES = {
    "bash": ({"command": "echo minimal-smoke"},
             {"command": "echo full-smoke && pwd && date"}),
    "bg_task": ({"action": "list"},
                {"action": "start", "command": "echo 'test'", "description": "test_smoke", "restart_on_failure": "false"}),
    "cd": ({}, {"path": "."}),
    "read_file": ({"path": "../config/config.json"}, {"path": "../config/config.json"}),
    "write_file": ({"path": SCRATCH, "content": "hello smoke\n"},
                   {"path": SCRATCH, "content": "hello smoke\n", "skip_validation": False}),
    "edit_file": ({"path": SCRATCH, "old_string": "hello", "new_string": "bye"},
                  {"path": SCRATCH, "old_string": "bye", "new_string": "hello",
                   "skip_validation": False}),
    "list_dir": ({"path": "."},
                 {"path": ".", "maxdepth": 2, "max_entries": 20, "show_hidden": False}),
    "lint_code": ({"content": "print('ok')", "lang": "python"},
                  {"content": "def f():\n    return 1\n", "path": "../vishva/paths.py",
                   "lang": "python"}),
    "read_cache": ({"path": "data/agent_cache/missing/missing.json"},
                   {"path": "data/agent_cache/missing/missing.json", "chunk": 0}),
    "web_read": ({"url": "https://example.com"},
                 {"url": "https://example.com", "wait_for": "", "max_chars": 2000,
                  "cache_fallback": True, "timeout": 20, "screenshot": False,
                  "js_render": True, "stealth": True}),
    "web_search": ({"query": "linux"}, {"query": "linux", "max_results": 3}),
    "product_search": ({"query": "ssd"},
                       {"query": "ssd", "sources": "geizhals,ddg", "max_results": 2}),
    "news_digest": ({"topics": ["technology"]}, {"topics": ["technology", "science"]}),
    "weather": ({"period": "today"}, {"period": "week"}),
    "spotify": ({"action": "stop"}, {"action": "play", "query": "rock"}),
    "speak": ({"speech": "Smoke test."}, {"speech": "Smoke test."}),
    "sched_task": ({"trigger_time": "2030-01-01 00:00:00", "prompt": "smoke test - ignore"},
                   {"trigger_time": "2030-01-01 00:00:00", "prompt": "smoke test - ignore"}),
    "ls_tasks": ({}, {}),
    "cancel_task": ({"task_id": "nonexistent"}, {"task_id": "nonexistent"}),
    "sys_update": ({}, {}),
    "sys_clean": ({}, {}),
    "vol_ctl": ({"action": "vol_up"}, {"action": "vol_percent", "percent": 50}),
    "brightness_ctl": ({"action": "up"}, {"action": "percent", "value": 80}),
    "turnoff_screen": ({}, {}),
    "subagent": ({"task": "Reply with the single word OK."},
                 {"task": "Reply with the single word OK.", "max_turns": 1}),
    "rag_search": ({"query": "smoke"}, {"query": "smoke", "top_k": 3}),
    "rag_save": ({"text": "smoke test entry"},
                 {"text": "smoke test entry", "category": "miscellaneous",
                  "priority": 1, "source": "smoke_test"}),
    "rag_reindex": ({}, {}),
    "rag_delete": ({"source": "smoke_test"}, {"source": "smoke_test"}),
    "rag_update": ({"query": "smoke", "text": "smoke updated"},
                   {"query": "smoke", "text": "smoke updated",
                    "category": "miscellaneous", "priority": 1}),
    "send_image": ({"path": "../assets/vishva.png"},
                   {"path": "../assets/vishva.png", "caption": "smoke"}),
    "send_file": ({"file_path": SCRATCH}, {"file_path": SCRATCH}),

    "mcp__pycode__show_current_path": ({}, {}),
    "mcp__pycode__get_time": ({}, {}),
    "mcp__pycode__get_date": ({}, {}),
    "mcp__pycode__execute": ({"bashprompt": "echo mcp-smoke"},
                             {"bashprompt": "echo mcp-smoke && uname -a", "timeout": 30}),
    "mcp__pycode__read_file": ({"pfad": "config/config.json"},
                               {"pfad": "config/config.json", "max_chars": 1000}),
    "mcp__pycode__list_dir": ({}, {"pfad": "."}),
    "mcp__pycode__create_file": ({"path": "tmp/mcp_smoke.txt", "context": "mcp"},
                                 {"path": "tmp/mcp_smoke.txt", "context": "mcp full"}),
    "mcp__pycode__append_file": ({"path": "tmp/mcp_smoke.txt", "context": "+append"},
                                 {"path": "tmp/mcp_smoke.txt", "context": "+append full"}),
    "mcp__pycode__search": ({"pattern": "model"},
                            {"pattern": "model", "pfad": "config",
                             "case_insensitive": True}),}


# ------------------------------------------------- Schema-Fallback für unbekannte Tools
def _placeholder(prop):
    if "enum" in prop:
        return prop["enum"][0]
    t = prop.get("type", "string")
    return {"integer": 1, "number": 1.0, "boolean": True,
            "array": ["test"], "object": {}}.get(t, "test")

def schema_min(schema):
    props = schema.get("properties", {})
    return {k: _placeholder(props[k]) for k in schema.get("required", []) if k in props}

def schema_full(schema):
    return {k: _placeholder(v) for k, v in schema.get("properties", {}).items()}

def _setup_edit(tm):
    tm.execute_tool("write_file", {"path": SCRATCH, "content": "hello smoke\n",
                                   "skip_validation": True})
def _setup_readcache(tm):
    tm.execute_tool("bash", {"command": "mkdir -p ../data/agent_cache/missing"})
    tm.execute_tool("write_file", {"path": "../data/agent_cache/missing/missing.json", "content": "{\"result\": \"test\", \"name\": \"test\"}", "skip_validation": True})

SETUP = {"edit_file": _setup_edit, "read_cache": _setup_readcache}


def run(tm, name, args, label):
    try:
        res = tm.execute_tool(name, copy.deepcopy(args))
    except Exception as e:
        res = {"error": f"{type(e).__name__}: {e}"}
    ok = isinstance(res, dict) and not res.get("error")
    err = "" if ok else f" | {res.get('error')[:70]}"
    print(f"[{'OK ' if ok else 'ERR'}] {name:38s} ({label}){err}")
    return "ok" if ok else "err"

def _run_scheduler_flow(tm, stats):
    marker_prompt = "smoke-test-scheduled-task"
    trigger = "2030-01-01 00:00:00"

    try:
        create_res = tm.execute_tool("sched_task", {
            "trigger_time": trigger,
            "prompt": marker_prompt,})
    except Exception as e:
        create_res = {"error": f"{type(e).__name__}: {e}"}

    create_ok = isinstance(create_res, dict) and create_res.get("success")
    task_id = create_res.get("task_id") if create_ok else None
    err = "" if create_ok else f" | {str(create_res.get('error'))[:70]}"
    print(f"[{'OK ' if create_ok else 'ERR'}] {'sched_task (create)':38s} (minimal){err}")
    stats["ok" if create_ok else "err"] += 1

    if not create_ok or not task_id:
        print(f"[ERR] ls_tasks / cancel_task skipped (create failed)")
        stats["err"] += 2
        return

    try:
        list_res = tm.execute_tool("ls_tasks", {
            "query": marker_prompt,
            "target": create_res.get("target") or "",
            "limit": 10,})
    except Exception as e:
        list_res = {"error": f"{type(e).__name__}: {e}"}

    list_ok = (isinstance(list_res, dict)
               and list_res.get("success")
               and any(t.get("task_id") == task_id for t in list_res.get("tasks", [])))
    err = "" if list_ok else f" | {str(list_res.get('error', 'task not in list'))[:70]}"
    print(f"[{'OK ' if list_ok else 'ERR'}] {'ls_tasks (filter)':38s} (full){err}")
    stats["ok" if list_ok else "err"] += 1

    try:
        cancel_res = tm.execute_tool("cancel_task", {"task_id": task_id})
    except Exception as e:
        cancel_res = {"error": f"{type(e).__name__}: {e}"}

    cancel_ok = isinstance(cancel_res, dict) and cancel_res.get("success")
    err = "" if cancel_ok else f" | {str(cancel_res.get('error'))[:70]}"
    print(f"[{'OK ' if cancel_ok else 'ERR'}] {'cancel_task (delete)':38s} (minimal){err}")
    stats["ok" if cancel_ok else "err"] += 1

    try:
        verify_res = tm.execute_tool("ls_tasks", {"query": marker_prompt})
    except Exception:
        verify_res = {"tasks": []}
    gone = task_id not in {t.get("task_id") for t in verify_res.get("tasks", [])}
    print(f"[{'OK ' if gone else 'ERR'}] {'ls_tasks (verify-gone)':38s} (full)")
    stats["ok" if gone else "err"] += 1

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dangerous", action="store_true",
                    help="auch sys_update/sys_clean/turnoff_screen ausführen")
    ap.add_argument("--only", default="", help="Komma-Liste: nur diese Tools testen")
    a = ap.parse_args()
    tm = ToolManager(config=load_config())
    pool = tm.available_tools
    only = set(x.strip() for x in a.only.split(",") if x.strip())
    stats = {"ok": 0, "err": 0, "skip": 0}

    for tool in pool:
        name = tool["function"]["name"]
        if only and name not in only:
            continue
        if name in SCHED_FLOW:
            continue
        if name in FIRST_RUN_ONLY:
            print(f"[SKIP] {name:38s} (first-run only — would fill essential slots)")
            stats["skip"] += 2
            continue
        if name in DANGEROUS and not a.dangerous:
            print(f"[SKIP] {name:38s} (dangerous -> --dangerous)")
            stats["skip"] += 2
            continue

        min_args, full_args = CASES.get(name, (None, None))
        if min_args is None:
            schema = tool["function"].get("parameters", {})
            min_args, full_args = schema_min(schema), schema_full(schema)

        if name in SETUP:
            SETUP[name](tm)

        stats[run(tm, name, min_args, "minimal")] += 1
        stats[run(tm, name, full_args, "full")] += 1

    sched_requested = (not only) or (SCHED_FLOW & only) == SCHED_FLOW
    if sched_requested and not only:
        _run_scheduler_flow(tm, stats)
    elif only and (SCHED_FLOW & only):
        _run_scheduler_flow(tm, stats)

    try:
        tm.execute_tool("rag_delete", {"source": "smoke_test"})
    except Exception:
        pass
    if getattr(tm, "mcp", None):
        tm.mcp.shutdown()

    print(f"\n=== SUMMARY: {stats['ok']} ok / {stats['err']} err / {stats['skip']} skipped ===")
    return 0 if stats["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
