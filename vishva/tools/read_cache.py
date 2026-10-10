"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
#import re
import json
#import time
#import shutil
#import subprocess
#from typing import Any, Dict, List, Optional, Tuple
from typing import Any, Dict, List
#from ..paths import p, cfg_path, BASE_DIR
from ..paths import p


def _read_cache(self, args: Dict[str, Any]) -> Dict[str, Any]:
    path = args.get("path", "")
    chunk = args.get("chunk")
    search = str(args.get("search", "") or "").strip()
    if not path:
        return {"error": "path is required."}
    base = p("data", "agent_cache")
    abs_path = os.path.abspath(path)
    if not (abs_path.startswith(base + os.sep) and abs_path.endswith(".json")):
        return {"error": "Invalid path. Only data/agent_cache/*.json allowed."}

    # ---------- Chunk-Modus (hat Vorrang vor search) ----------
    if chunk is not None:
        try:
            chunk = int(chunk)
        except (TypeError, ValueError):
            return {"error": "chunk must be an integer."}
        chunk_path = abs_path[:-len(".json")] + f"__c{chunk}.json"
        if not os.path.isfile(chunk_path):
            n, stem = 0, abs_path[:-len(".json")]
            while os.path.isfile(f"{stem}__c{n}.json"):
                n += 1
            if n == 0:
                return {"error": "No chunks available for this path."}
            return {"error": f"Chunk {chunk} not found. Valid: 0..{n - 1}"}
        try:
            with open(chunk_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            resp = {
                "success": True,
                "chunk": data.get("chunk", chunk),
                "total": data.get("total", "?"),
                "content": data.get("result", "")}
            if chunk + 1 < (data.get("total") or 0):
                resp["next"] = chunk + 1
            return resp
        except Exception as e:
            return {"error": str(e)}

    if not os.path.isfile(abs_path):
        return {"error": f"File not found: {path}"}
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            content = json.load(f)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {str(e)}"}
    except Exception as e:
        return {"error": str(e)}

    if content.get("type") == "chunk":
        return {"success": True, "chunk": content.get("chunk"),
                "total": content.get("total"),
                "content": content.get("result", "")}

    result_text = content.get("result", "")
    n_chunks = content.get("chunks", 0) or 0

    # ---------- Keyword-Suche ----------
    if search:
        keywords = [kw.lower() for kw in search.split() if kw.strip()]
        if not keywords:
            return {"error": "Empty search query."}

        def _excerpt(text: str, kws: List[str],
                     max_lines: int = 12, ctx: int = 1):
            """Returns (hit_count, excerpt_of_matching_lines_with_context)."""
            lines = text.splitlines()
            hit = {i for i, ln in enumerate(lines)
                   if any(kw in ln.lower() for kw in kws)}
            if not hit:
                return 0, ""
            show = set()
            for i in hit:
                for j in range(max(0, i - ctx), min(len(lines), i + ctx + 1)):
                    show.add(j)
            ordered = sorted(show)
            if len(ordered) > max_lines:
                half = max_lines // 2
                ordered = ordered[:half] + ordered[-half:]
            return len(hit), "\n".join(lines[i] for i in ordered)[:1500]

        # Ohne Chunks: Volltext durchsuchen → bei Treffer kompletter Inhalt
        if not n_chunks:
            if any(kw in result_text.lower() for kw in keywords):
                return {"success": True, "found": 1, "search": search,
                        "name": content.get("name", ""),
                        "content": result_text}
            return {"success": True, "found": 0, "search": search,
                    "message": f"No matches for '{search}' in this cache entry."}

        # Mit Chunks: nur relevante Chunks zurückgeben
        stem = abs_path[:-len(".json")]
        matches = []
        n = 0
        while os.path.isfile(f"{stem}__c{n}.json"):
            try:
                with open(f"{stem}__c{n}.json", "r", encoding="utf-8") as cf:
                    cdata = json.load(cf)
                ctext = cdata.get("result", "")
                hits, excerpt = _excerpt(ctext, keywords)
                if hits > 0:
                    matches.append({
                        "chunk": n,
                        "hits": hits,
                        "excerpt": excerpt,
                        "read_full": f"chunk={n}"})
            except Exception:
                pass
            n += 1

        if not matches:
            return {"success": True, "found": 0, "search": search,
                    "chunks_scanned": n,
                    "message": f"No matches for '{search}' in {n} chunks."}

        matches.sort(key=lambda m: m["hits"], reverse=True)
        return {"success": True,
                "search": search,
                "found": len(matches),
                "chunks_scanned": n,
                "matches": matches[:10]}

    # ---------- Normaler Lese-Modus ----------
    if n_chunks:
        return {
            "success": True,
            "name": content.get("name", ""),
            "length": len(result_text),
            "preview": result_text[:400],
            "chunks": n_chunks,
            "message": (f"Result is too large for one response. "
                        f"Read it in parts: chunk=0..{n_chunks - 1}, "
                        f"or use search='<keywords>' to find specific content.")}
    return {
        "success": True,
        "content": result_text,
        "name": content.get("name", ""),
        "arguments": content.get("arguments", "")}
