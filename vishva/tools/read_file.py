"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
import tempfile
import shutil
import subprocess
from typing import Any, Dict


def _read_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
    path = self._resolve_path(args.get("path", ""))
    if not path:
        return {"error": "path is required."}
    if not os.path.exists(path):
        return {"error": f"File not found: {path}"}
    if path.lower().endswith(".pdf"):
        return _read_pdf(self, path)

    start_line = args.get("start_line")
    end_line = args.get("end_line")
    show_numbers = bool(args.get("show_line_numbers", False))
    range_requested = start_line is not None

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return {"error": str(e)}

    lines = content.split("\n")
    total = len(lines)
    size = len(content.encode("utf-8"))

    # ---------- Zeilenbereich (umgeht das Byte-Limit) ----------
    if range_requested:
        try:
            s = int(start_line)
            e = int(end_line) if end_line is not None else s
        except (TypeError, ValueError):
            return {"error": "start_line/end_line must be integers."}
        if s < 1 or s > total or e < s:
            return {"error": f"Invalid range {s}-{e}. File has {total} lines."}
        e = min(e, total)
        selected = lines[s - 1:e]
        if show_numbers:
            width = len(str(e))
            out = "\n".join(f"{s + i:>{width}} | {ln}" for i, ln in enumerate(selected))
        else:
            out = "\n".join(selected)
        return {"success": True, "content": out,
                "total_lines": total, "range": f"{s}-{e}", "size": size}

    # ---------- Größen-Limit nur bei Voll-Reads ----------
    MAX_READ_BYTES = self.config.get("max_read_file_bytes", 1024)
    if size > MAX_READ_BYTES:
        head = "\n".join(lines[:100])
        tail = content[-5000:]
        return {
            "success": True,
            "content": (head
                        + f"\n\n... [File too large: {size} bytes, head/tail only. "
                          f"Use start_line/end_line (+show_line_numbers) for specific ranges] ...\n\n"
                        + tail),
            "truncated": True, "size": size, "total_lines": total}

    # ---------- Voll-Read ----------
    if show_numbers:
        width = len(str(total))
        out = "\n".join(f"{i + 1:>{width}} | {ln}" for i, ln in enumerate(lines))
    else:
        out = content
    return {"success": True, "content": out, "total_lines": total, "size": size}


def _read_pdf(self, path: str) -> Dict[str, Any]:
    ocr_enabled = bool(self.config.get("pdf_ocr_enabled", True))
    ocr_lang = self.config.get("pdf_ocr_language", "deu")
    ocr_timeout = int(self.config.get("pdf_ocr_timeout", 300))
    max_pages = int(self.config.get("pdf_max_pages", 50))
    max_chars = int(self.config.get("pdf_max_chars", 50000))

    try:
        from pypdf import PdfReader
    except ImportError:
        return {"error": "pypdf not installed. Run: pip install pypdf"}

    tmp_dir = tempfile.mkdtemp()
    try:
        processed_pdf = path
        if ocr_enabled and shutil.which("ocrmypdf"):
            ocr_output = os.path.join(tmp_dir, "ocr_output.pdf")
            try:
                result = subprocess.run(
                    ["ocrmypdf", "-l", ocr_lang, "--force-ocr", "--skip-text",
                     path, ocr_output],
                    capture_output=True,
                    text=True,
                    timeout=ocr_timeout
                )
                if result.returncode == 0 and os.path.exists(ocr_output):
                    processed_pdf = ocr_output
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                pass

        reader = PdfReader(processed_pdf)
        total_pages = len(reader.pages)
        pages_to_read = min(total_pages, max_pages)
        text_parts = []
        total_chars = 0
        for i in range(pages_to_read):
            page_text = reader.pages[i].extract_text()
            if page_text:
                page_content = f"\n--- Page {i+1}/{total_pages} ---\n{page_text}"
                if total_chars + len(page_content) > max_chars:
                    text_parts.append(f"\n... [PDF zu lang, nach {i} Seiten abgebrochen] ...")
                    break
                text_parts.append(page_content)
                total_chars += len(page_content)

        content = "\n".join(text_parts)

        return {
            "success": True,
            "content": content,
            "pages": total_pages,
            "pages_read": pages_to_read,
            "ocr_applied": processed_pdf != path
        }

    except Exception as e:
        return {"error": f"PDF reading failed: {type(e).__name__}: {e}"}
    finally:
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            pass
