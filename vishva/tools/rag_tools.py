"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
import re
import json
import time
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple
try:
    import requests
except ImportError:
    requests = None

def _rag_mgr(self):
    """RAGManager auflösen: direktes Attribut → agent_ref → lazy erzeugen."""
    mgr = getattr(self, "rag_manager", None)
    if mgr:
        return mgr
    # Fallback 1: der Agent hängt am ToolManager (agent_ref)
    agent = getattr(self, "agent_ref", None)
    mgr = getattr(agent, "rag_manager", None) if agent else None
    if mgr:
        self.rag_manager = mgr          # cachen, damit alle Tools dieselbe Instanz nutzen
        return mgr
    # Fallback 2: standalone (test_tool.sh) → eigene Instanz erzeugen
    if not bool(self.config.get("rag_enabled", True)):
        return None
    try:
        from ..rag_manager import RAGManager
        mgr = RAGManager(config=self.config)
        self.rag_manager = mgr
        return mgr
    except Exception:
        return None

def _rag_save(self, args: Dict[str, Any]) -> Dict[str, Any]:
    mgr = self._rag_mgr()
    if not mgr:
        return {"error": "RAG Manager not available."}
    text = args.get("text", "")
    if not text:
        return {"error": "text is required."}
    category = args.get("category", "miscellaneous")
    reserved = {str(x).lower() for x in self.config.get("rag_reserved_categories", ["essential", "knowledge"])}
    if str(category).lower() in reserved:
        category = str(self.config.get("rag_category_fallback", "miscellaneous")).lower()
    metadata = {}
    if "priority" in args:
        try:
            metadata["priority"] = max(0, min(3, int(args.get("priority"))))
        except (ValueError, TypeError):
            pass
    return mgr.add_text(
        text,
        source=args.get("source", "manual"),
        category=category,
        metadata=metadata if metadata else None)

def _rag_essential(self, args: Dict[str, Any]) -> Dict[str, Any]:
    mgr = self._rag_mgr()
    if not mgr:
        return {"error": "RAG Manager not available."}
    marker_path = "data/.first_run_done"
    if os.path.exists(marker_path):
        return {"error": "Essential entries can only be created during first-run setup."}
    text = args.get("text", "").strip()
    if not text:
        return {"error": "text is required."}
    essentials = [e for e in mgr.entries if str(e.get("category", "")).lower() == "essential"]
    if len(essentials) >= 3:
        return {"error": f"Maximum of 3 essential entries already reached ({len(essentials)}/3)."}
    result = mgr.add_text(text, source="first_run", category="essential")
    if result.get("success"):
        return {"success": True, "message": f"Essential entry created ({len(essentials) + 1}/3)."}
    return result

def _rag_search(self, args: Dict[str, Any]) -> Dict[str, Any]:
    mgr = self._rag_mgr()
    if not mgr:
        return {"error": "RAG Manager not available."}
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query is required."}
    res = mgr.search(query, top_k=args.get("top_k"))
    results = res.get("results", []) if isinstance(res, dict) else []
    slim = [{
        "id": r.get("id"),
        "score": round(float(r.get("score", 0)), 4),
        "text": r.get("text", ""),
        "category": r.get("category", ""),
        "source": r.get("source", ""),} for r in results]
    return {"success": True, "count": len(slim), "results": slim}

def _rag_reindex(self, args: Dict[str, Any]) -> Dict[str, Any]:
    mgr = self._rag_mgr()
    if not mgr:
        return {"error": "RAG Manager not available."}
    return mgr.index_knowledge_folder(verbose=True)

def _rag_delete(self, args: Dict[str, Any]) -> Dict[str, Any]:
    mgr = self._rag_mgr()
    if not mgr:
        return {"error": "RAG Manager not available."}
    entry_id = args.get("id", "")
    source_prefix = args.get("source", "")
    force = bool(args.get("force", False))
    if not entry_id and not source_prefix:
        return {"error": "id or source is required."}
    essential = {str(c).lower() for c in self.config.get("rag_essential_categories", ["essential"])}
    def guarded(e):
        return str(e.get("category", "")).lower() in essential
    before = len(mgr.entries)
    skipped = 0
    keep = []
    for e in mgr.entries:
        hit = (entry_id and e.get("id") == entry_id) or \
              (source_prefix and str(e.get("source", "")).startswith(source_prefix))
        if hit:
            if not force and guarded(e):
                keep.append(e); skipped += 1
        else:
            keep.append(e)
    mgr.entries = keep
    removed = before - len(mgr.entries)
    if removed:
        mgr._rebuild_entry_index()
        for name in ("_sync_arrays_to_entries", "_build_arrays_from_entries"):
            fn = getattr(mgr, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
                break
        if hasattr(mgr, "_score_cache"):
            mgr._score_cache = None
        mgr._save()
    return {"success": True, "removed": removed, "skipped_protected": skipped}

def _rag_update(self, args: Dict[str, Any]) -> Dict[str, Any]:
    mgr = self._rag_mgr()
    if not mgr:
        return {"error": "RAG Manager not available."}
    entry = None
    entry_id = str(args.get("id", "") or "")
    query = (args.get("query", "") or "").strip()
    if entry_id:
        entry = mgr._entry_by_id.get(entry_id)
    elif query:
        res = mgr.query_no_track(query, top_k=1, min_score=0.0)
        results = res.get("results", []) if isinstance(res, dict) else []
        if results:
            entry = mgr._entry_by_id.get(str(results[0].get("id", "")))
    if not entry:
        return {"error": "Entry not found. Provide a valid 'id' or a matching 'query'."}

    new_text = (args.get("text", "") or "").strip()
    new_category = args.get("category")
    new_priority = args.get("priority")
    if not new_text and not new_category and new_priority is None:
        return {"error": "Nothing to update. Provide 'text', 'category', or 'priority'."}
    source = entry.get("source", "manual")
    old_id = entry.get("id")
    old_text = entry.get("text", "")
    keep_category = entry.get("category", "miscellaneous")
    keep_priority = entry.get("priority")
    keep_access = entry.get("access_count", 0)
    keep_meta = dict(entry.get("metadata", {}) or {})
    keep_created = entry.get("created_at")
    final_text = new_text if new_text else old_text
    final_category = new_category if new_category else keep_category
    final_priority = new_priority if new_priority is not None else keep_priority
    reserved = {str(x).lower() for x in
                self.config.get("rag_reserved_categories", ["essential", "knowledge"])}
    if str(final_category).lower() in reserved and \
       str(final_category).lower() != str(keep_category).lower():
        final_category = str(self.config.get("rag_category_fallback", "miscellaneous")).lower()
    mgr.entries = [e for e in mgr.entries if e.get("id") != old_id]
    mgr._rebuild_entry_index()
    for name in ("_sync_arrays_to_entries", "_build_arrays_from_entries"):
        fn = getattr(mgr, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass
            break
    add_res = mgr.add_text(final_text, source=source, category=final_category)
    if not add_res.get("success") or add_res.get("added", 0) == 0:
        return {"error": "Re-adding the entry failed.", "detail": add_res}
    new_entry = None
    try:
        new_entry = mgr._entry_by_id.get(str(mgr._entry_id(source, final_text)))
    except Exception:
        new_entry = None
    if not new_entry:
        for e in mgr.entries:
            if str(e.get("text", "")).strip() == final_text and e.get("source") == source:
                new_entry = e
                break
    if new_entry:
        if final_priority is not None:
            try:
                new_entry["priority"] = max(0, min(3, int(final_priority)))
            except (ValueError, TypeError):
                pass
        new_entry["access_count"] = keep_access
        new_entry["maintained"] = True
        if keep_meta:
            new_entry.setdefault("metadata", {}).update(keep_meta)
        if keep_created:
            new_entry["created_at"] = keep_created
    try:
        mgr._save()
    except Exception:
        pass
    return {
        "success": True,
        "old_id": old_id,
        "new_id": new_entry.get("id") if new_entry else None,
        "text": final_text[:120],
        "category": final_category,}
