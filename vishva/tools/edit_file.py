"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import json
from typing import Any, Dict

try:
    import requests
except ImportError:
    requests = None


def _edit_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
    path = self._resolve_path(args.get("path", ""))
    if not path:
        return {"error": "path is required."}
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return {"error": f"Could not read file: {e}"}

    old_string = args.get("old_string")
    new_string = args.get("new_string")
    start_line = args.get("start_line")
    end_line = args.get("end_line")
    skip_validation = args.get("skip_validation", False)

    new_content = None
    match_note = ""

    if start_line is not None:
        try:
            start_line = int(start_line)
            end_line = int(end_line) if end_line is not None else start_line
        except (TypeError, ValueError):
            return {"error": "start_line and end_line must be integers."}
        lines = content.split("\n")
        total = len(lines)
        if start_line < 1 or start_line > total or end_line < start_line:
            return {
                "error": f"Invalid line range {start_line}-{end_line}. File has {total} lines.",
                "file_content": content,
                "suggestion": "Use read_file with show_line_numbers=true to see exact line numbers."}
        end_line = min(end_line, total)
        if new_string is None:
            return {"error": "new_string is required (use an empty string to delete the lines)."}
        replacement = [] if new_string == "" else new_string.split("\n")
        new_lines = lines[:start_line - 1] + replacement + lines[end_line:]
        new_content = "\n".join(new_lines)
        match_note = f"line range {start_line}-{end_line} replaced"

    else:
        if old_string is None or new_string is None:
            return {
                "error": "Provide either (old_string + new_string) or (start_line [+ end_line] + new_string).",
                "file_content": content}

        if old_string in content:
            new_content = content.replace(old_string, new_string, 1)
            match_note = "exact match"
        else:
            def _normalize_ws(s: str) -> str:
                return "\n".join(line.strip() for line in s.strip().split("\n"))
            norm_old = _normalize_ws(old_string)
            norm_content = _normalize_ws(content)
            if norm_old in norm_content:
                old_lines = old_string.strip().split("\n")
                content_lines = content.split("\n")
                for i in range(len(content_lines) - len(old_lines) + 1):
                    chunk = content_lines[i:i + len(old_lines)]
                    if _normalize_ws("\n".join(chunk)) == norm_old:
                        new_lines = (content_lines[:i]
                                     + new_string.split("\n")
                                     + content_lines[i + len(old_lines):])
                        new_content = "\n".join(new_lines)
                        match_note = f"whitespace-normalized match (line {i + 1})"
                        break
            if new_content is None:
                anchor = old_string.strip()[:50]
                if anchor and anchor in content:
                    idx = content.find(anchor)
                    context_start = max(0, idx - 200)
                    context_end = min(len(content), idx + 200)
                    return {
                        "error": f"'old_string' not found exactly, but anchor '{anchor[:30]}...' found. Context does not match.",
                        "file_content": content,
                        "context": content[context_start:context_end],
                        "suggestion": "Match exactly — or use start_line/end_line for line-based editing."}
                return {
                    "error": f"'old_string' not found in '{path}' (exact, whitespace-normalized, or fuzzy).",
                    "file_content": content,
                    "suggestion": "Use read_file to see current content — or use start_line/end_line."}

    if new_content == content:
        return {"success": True,
                "message": "No change applied (replacement equals original)."}

    lang = self._detect_language(path, new_content)
    lint_result = None
    if not skip_validation and lang in ("python", "json", "bash"):
        lint_result = self._lint_content(path, new_content, lang)
        if not lint_result["valid"]:
            return {
                "error": f"Syntax validation failed for {lang} — edit NOT applied.",
                "lint": lint_result,
                "suggestion": "Fix new_string so the resulting file is valid, or set skip_validation=true."}

    backup_path = self._backup_file(path)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        return {"error": f"Write failed: {e}"}
    resp = {"success": True,
            "message": f"File '{path}' edited ({match_note}).",
            "lang": lang}
    if backup_path:
        resp["backup"] = backup_path
    if lint_result:
        resp["lint"] = lint_result
    return resp
