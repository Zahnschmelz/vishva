"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
import re
import json
import time
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple
from ..paths import p, cfg_path, BASE_DIR

def _read_cache(self, args: Dict[str, Any]) -> Dict[str, Any]:
    path = args.get("path", "")
    chunk = args.get("chunk")
    if not path:
        return {"error": "path is required."}
    base = p("data", "agent_cache")
    abs_path = os.path.abspath(path)
    if not (abs_path.startswith(base + os.sep) and abs_path.endswith(".json")):
        return {"error": "Invalid path. Only data/agent_cache/*.json allowed."}
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
        if content.get("type") == "chunk":
            return {"success": True, "chunk": content.get("chunk"),
                    "total": content.get("total"), "content": content.get("result", "")}
        result_text = content.get("result", "")
        n_chunks = content.get("chunks", 0) or 0
        if n_chunks:
            return {
                "success": True,
                "name": content.get("name", ""),
                "length": len(result_text),
                "preview": result_text[:400],
                "chunks": n_chunks,
                "message": f"Result is too large for one response. Read it in parts: chunk=0..{n_chunks - 1}."}
        return {
            "success": True,
            "content": result_text,
            "name": content.get("name", ""),
            "arguments": content.get("arguments", "")}
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {str(e)}"}
    except Exception as e:
        return {"error": str(e)}
