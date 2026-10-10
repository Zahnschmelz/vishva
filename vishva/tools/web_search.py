"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
#import os
#import re
#import json
#import time
#import shutil
#import subprocess
#from typing import Any, Dict, List, Optional, Tuple
from typing import Any, Dict

try:
    import requests
except ImportError:
    requests = None


def _web_search(self, args: Dict[str, Any]) -> Dict[str, Any]:
    query = args.get("query", "")
    max_results = min(args.get("max_results", 5), 10)
    if not query:
        return {"error": "query is required."}
    try:
        from ddgs import DDGS
    except ImportError:
        return {
            "error": "ddgs not installed. Run: pip install ddgs"}
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, region="de-de", max_results=max_results))
        if not results:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return {"success": True, "results": [], "message": "No results found."}
        formatted = []
        for r in results:
            formatted.append({
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", "")[:200]})
        return {
            "success": True,
            "query": query,
            "count": len(formatted),
            "results": formatted}
    except Exception as e:
        return {"error": f"Search failed: {type(e).__name__}: {str(e)}"}

