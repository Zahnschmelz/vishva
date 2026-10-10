"""Code-to-File: extrahiert Code-Blöcke aus Antworten, speichert sie,
validiert Syntax und liefert Auto-Fix-Prompts. Frontend-unabhängig."""
import os
import re
import json
import random
import string
import shutil
import subprocess
from typing import Dict, List, Any, Optional
from .paths import cfg_path

CODE_BLOCK_RE = re.compile(
    r"^[ \t]*```([\w+-]*)[ \t]*\r?\n"
    r"(.*?)"
    r"\n?[ \t]*```[ \t]*$",
    re.DOTALL | re.MULTILINE)

LANG_TO_EXT = {
    "python": ".py", "py": ".py",
    "bash": ".sh", "sh": ".sh", "shell": ".sh", "zsh": ".sh",
    "javascript": ".js", "js": ".js", "node": ".js",
    "typescript": ".ts", "ts": ".ts",
    "json": ".json", "jsonl": ".jsonl", "json5": ".json5", "jsonc": ".jsonc",
    "yaml": ".yaml", "yml": ".yaml", "toml": ".toml", "ini": ".ini",
    "xml": ".xml", "html": ".html", "css": ".css", "sql": ".sql",
    "markdown": ".md", "md": ".md", "txt": ".txt", "text": ".txt",
    "c": ".c", "h": ".h", "cpp": ".cpp", "c++": ".cpp", "java": ".java",
    "rust": ".rs", "go": ".go", "ruby": ".rb", "php": ".php",
    "dockerfile": ".dockerfile", "makefile": ".makefile",}

class CodeManager:
    def __init__(self, config: Dict[str, Any], save_handler=None,
                 debug_level_fn=None, log_fn=None):
        self.config = config or {}
        self.save_handler = save_handler
        # Optionale Callbacks für Debug-Ausgaben (kommen vom Agent)
        self._debug_level_fn = debug_level_fn or (lambda: 0)
        self._log_fn = log_fn or (lambda msg: None)

    def _debug_level(self) -> int:
        try:
            return int(self._debug_level_fn() or 0)
        except Exception:
            return 0

    def set_handler(self, fn):
        """Frontend-Handler: fn(block, idx, total) -> {'save', 'filename', 'skip_all'}"""
        self.save_handler = fn

    # ---------- Extraktion ----------
    def extract_code_blocks(self, text: str) -> List[Dict[str, str]]:
        return [{"lang": (m.group(1) or "text").lower().strip(),
                 "code": m.group(2)}
                for m in CODE_BLOCK_RE.finditer(text)]

    # ---------- Filename ----------
    def default_code_filename(self, blocks: List[Dict]) -> str:
        lang = blocks[0]["lang"] if blocks else "text"
        ext = LANG_TO_EXT.get(lang, ".txt")
        rand_str = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
        return f"snippet_{rand_str}{ext}"

    # ---------- Speichern ----------
    def save_code_to_file(self, blocks: List[Dict], filename: str) -> Optional[str]:
        code_dir = cfg_path(self.config, "code_to_file_dir", "data/code_snippets")
        os.makedirs(code_dir, exist_ok=True)
        safe = os.path.basename(filename or "").strip()
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", safe)
        if not safe:
            safe = self.default_code_filename(blocks)
        if not os.path.splitext(safe)[1]:
            safe += LANG_TO_EXT.get(blocks[0]["lang"], ".txt")
        base, ext = os.path.splitext(safe)
        candidate = os.path.join(code_dir, safe)
        i = 1
        while os.path.exists(candidate):
            candidate = os.path.join(code_dir, f"{base}_{i}{ext}")
            i += 1
        try:
            with open(candidate, "w", encoding="utf-8") as f:
                if len(blocks) == 1:
                    f.write(blocks[0]["code"])
                    if not blocks[0]["code"].endswith("\n"):
                        f.write("\n")
                else:
                    for idx, block in enumerate(blocks, 1):
                        f.write(f"# --- Block {idx} ({block['lang']}) ---\n")
                        f.write(block["code"])
                        if not block["code"].endswith("\n"):
                            f.write("\n")
                        f.write("\n")
            return candidate
        except Exception as e:
            if self._debug_level() >= 1:
                self._log_fn(f"[CodeSave] Write failed: {e}")
            return None

    # ---------- Orchestrator ----------
    def maybe_save(self, response: str) -> List[Dict[str, Any]]:
        if not bool(self.config.get("code_to_file_enabled", False)):
            return []
        if not self.save_handler:
            return []
        blocks = self.extract_code_blocks(response)
        if not blocks:
            return []
        min_lines = int(self.config.get("code_to_file_min_lines", 5) or 5)
        eligible = [b for b in blocks if b["code"].count("\n") + 1 >= min_lines]
        if not eligible:
            return []
        saved = []
        for idx, block in enumerate(eligible, 1):
            try:
                result = self.save_handler(block, idx, len(eligible))
            except Exception as e:
                if self._debug_level() >= 1:
                    self._log_fn(f"[CodeSave] Handler error: {e}")
                continue
            if not result:
                continue
            if result.get("skip_all"):
                break
            if not result.get("save"):
                continue
            filename = result.get("filename") or self.default_code_filename([block])
            path = self.save_code_to_file([block], filename)
            if path:
                entry = {"path": path, "validation": None}
                if bool(self.config.get("code_to_file_validate", True)):
                    entry["validation"] = self.validate_code_file(path)
                    v = entry["validation"]
                    if v["checked"] and not v["ok"] and self._debug_level() >= 1:
                        self._log_fn(f"[CodeSave] ❌ {path}: {v['error']}")
                saved.append(entry)
        return saved

    # ---------- Validierung ----------
    def validate_code_file(self, path: str) -> Dict[str, Any]:
        ext = os.path.splitext(path)[1].lower()
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return {"checked": False, "ok": True, "error": None}
        if ext == ".py":
            try:
                import ast
                ast.parse(content)
                return {"checked": True, "ok": True, "error": None}
            except SyntaxError as e:
                return {"checked": True, "ok": False,
                        "error": f"SyntaxError line {e.lineno}: {e.msg}"}
        if ext == ".sh":
            try:
                r = subprocess.run(["bash", "-n", path],
                                   capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    return {"checked": True, "ok": True, "error": None}
                return {"checked": True, "ok": False,
                        "error": (r.stderr or "").strip()[:500]}
            except Exception:
                return {"checked": False, "ok": True, "error": None}
        if ext == ".json":
            try:
                json.loads(content)
                return {"checked": True, "ok": True, "error": None}
            except Exception as e:
                return {"checked": True, "ok": False, "error": str(e)[:500]}
        if ext == ".jsonl":
            for i, line in enumerate(content.splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                except Exception as e:
                    return {"checked": True, "ok": False,
                            "error": f"Line {i}: {str(e)[:300]}"}
            return {"checked": True, "ok": True, "error": None}
        if ext in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError:
                return {"checked": False, "ok": True, "error": None}
            try:
                yaml.safe_load(content)
                return {"checked": True, "ok": True, "error": None}
            except Exception as e:
                return {"checked": True, "ok": False, "error": str(e)[:500]}
        if ext == ".js" and shutil.which("node"):
            try:
                r = subprocess.run(["node", "--check", path],
                                   capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    return {"checked": True, "ok": True, "error": None}
                return {"checked": True, "ok": False,
                        "error": (r.stderr or "").strip()[:500]}
            except Exception:
                return {"checked": False, "ok": True, "error": None}
        return {"checked": False, "ok": True, "error": None}

    # ---------- Auto-Fix-Prompt ----------
    def build_fix_prompt(self, failed: List[Dict[str, Any]]) -> str:
        parts = []
        for entry in failed:
            path = entry["path"]
            err = entry["validation"]["error"]
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()[:4000]
            except Exception:
                content = "(nicht lesbar)"
            parts.append(
                f"### File: {path}\n"
                f"Check-Error: {err}\n"
                f"Current content:\n```\n{content}\n```")
        return (
            "AUTO-FIX: The following saved code file(s) FAILED the syntax check.\n"
            "Analyze the error, fix the code, and write the corrected version "
            "back to the SAME path using write_file.\n"
            "Do not explain at length — just fix and save.\n\n"
            + "\n\n".join(parts))
