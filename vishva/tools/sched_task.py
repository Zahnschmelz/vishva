"""Scheduler-Tools: sched_task, ls_tasks, cancel_task."""
#import os
#import re
import json
#import time
from datetime import datetime
#from typing import Any, Dict, List, Optional, Tuple
from typing import Any, Dict, List, Optional

#from ..paths import p, cfg_path


def _get_scheduler(self):
    """TaskScheduler auflösen: direkter Ref → Agent-Ref → standalone lazy."""
    sched = getattr(self, "scheduler", None)
    if sched:
        return sched
    agent = getattr(self, "agent_ref", None)
    sched = getattr(agent, "scheduler", None) if agent else None
    if sched:
        self.scheduler = sched
        return sched
    try:
        from ..scheduler import TaskScheduler
        sched = TaskScheduler(self.config)
        self.scheduler = sched
        return sched
    except Exception as e:
        return None


def _sched_task(self, args: Dict[str, Any]) -> Dict[str, Any]:
    """Schedule a task for later execution with multi-channel delivery."""
    trigger_time = (args.get("trigger_time") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    target = (args.get("target") or "").strip() or None
    fallback_target = (args.get("fallback_target") or "").strip() or None
    chat_id = (args.get("chat_id") or "").strip() or None

    if not trigger_time:
        return {"error": "trigger_time is required (YYYY-MM-DD HH:MM:SS)."}
    if not prompt:
        return {"error": "prompt is required."}

    # target default based on current interface
    if not target:
        agent = getattr(self, "agent_ref", None)
        interface = getattr(agent, "interface", "") or ""
        if interface in ("telegram", "gui", "cli"):
            target = interface

    sched = _get_scheduler(self)
    if sched is None:
        return {"error": "Scheduler not available."}

    try:
        result = sched.add_task(
            prompt=prompt,
            trigger_time=trigger_time,
            target=target or "auto",
            fallback_target=fallback_target,
            chat_id=chat_id,
        )
    except Exception as e:
        return {"error": f"Scheduler.add_task failed: {type(e).__name__}: {e}"}

    if not result or result.get("error"):
        return result or {"error": "add_task returned no result"}
    return {
        "success": True,
        "task_id": result.get("task_id"),
        "trigger_time": result.get("trigger_time", trigger_time),
        "target": result.get("target", target),
        "prompt": prompt[:120],
    }


def _ls_tasks(self, args: Dict[str, Any]) -> Dict[str, Any]:
    """List scheduled tasks from data/scheduled_tasks.json.
    No session-id required; optional filters narrow the result set."""
    target = (args.get("target") or "").strip() or None
    status = (args.get("status") or "").strip() or None
    query = (args.get("query") or "").strip() or None
    chat_id = (args.get("chat_id") or "").strip() or None
    due_before = (args.get("due_before") or "").strip() or None
    due_after = (args.get("due_after") or "").strip() or None
    include_done = bool(args.get("include_done", False))
    try:
        limit = max(1, min(500, int(args.get("limit", 50) or 50)))
    except (TypeError, ValueError):
        limit = 50

    sched = _get_scheduler(self)
    if sched is None:
        return {"error": "Scheduler not available."}

    try:
        tasks = sched.list_tasks(chat_id=chat_id) or []
    except Exception as e:
        return {"error": f"Scheduler.list_tasks failed: {type(e).__name__}: {e}"}

    def _parse_dt(s: str) -> Optional[datetime]:
        if not s:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    before_dt = _parse_dt(due_before)
    after_dt = _parse_dt(due_after)
    if (due_before and before_dt is None) or (due_after and after_dt is None):
        return {"error": "due_before/due_after must be 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD'."}

    query_lower = query.lower() if query else None
    filtered: List[Dict[str, Any]] = []
    for t in tasks:
        t_status = (t.get("status") or "pending").lower()
        if not include_done and t_status in ("done", "failed"):
            continue
        if status and t_status != status.lower():
            continue
        if target and (t.get("target") or "").lower() != target.lower():
            continue
        if chat_id and str(t.get("chat_id", "")) != str(chat_id):
            continue
        if query_lower and query_lower not in (t.get("prompt") or "").lower():
            continue
        trig = _parse_dt(t.get("trigger_time") or "")
        if before_dt and (trig is None or trig >= before_dt):
            continue
        if after_dt and (trig is None or trig <= after_dt):
            continue
        filtered.append({
            "task_id": t.get("id") or t.get("task_id"),
            "trigger_time": t.get("trigger_time"),
            "target": t.get("target"),
            "status": t_status,
            "prompt": (t.get("prompt") or "")[:120],
            "chat_id": t.get("chat_id"),
            "created_at": t.get("created_at"),
        })

    filtered.sort(key=lambda x: x.get("trigger_time") or "")
    filtered = filtered[:limit]

    return {
        "success": True,
        "count": len(filtered),
        "filters": {k: v for k, v in {
            "target": target, "status": status, "query": query,
            "chat_id": chat_id, "due_before": due_before,
            "due_after": due_after, "include_done": include_done,
        }.items() if v},
        "tasks": filtered,
    }


def _cancel_task(self, args: Dict[str, Any]) -> Dict[str, Any]:
    """Cancel a scheduled task by id."""
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return {"error": "task_id is required."}

    sched = _get_scheduler(self)
    if sched is None:
        return {"error": "Scheduler not available."}

    try:
        ok = sched.cancel_task(task_id)
    except Exception as e:
        return {"error": f"Scheduler.cancel_task failed: {type(e).__name__}: {e}"}

    if ok:
        return {"success": True, "task_id": task_id, "message": "task cancelled"}
    return {"success": False, "task_id": task_id,
            "error": f"task '{task_id}' not found"}
