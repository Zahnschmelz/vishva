import os
#import re
import json
import math
import hashlib
import threading
from typing import List, Dict, Any, Optional
from .paths import p, cfg_path
try:
    import numpy as np
except ImportError:
    np = None
try:
    from .rag_storage import RagStorage
except Exception:
    RagStorage = None

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

class RAGManager:
    def __init__(self, config=None, knowledge_dir: str = None):
        self.config = config or {}
        self.knowledge_dir = knowledge_dir or p("knowledge")
        self.enabled = bool(self.config.get("rag_enabled", True))
        self.top_k = int(self.config.get("rag_top_k", 5))
        self.max_context_chars = int(self.config.get("rag_max_context_chars", 2500))
        self.entries: List[Dict[str, Any]] = []
        self._model = None
        self._st_failed = False
        self._access_save_counter = 0
        self._model_lock = threading.Lock()
        self._preload_done = False
        self.db_dir = cfg_path(self.config, "rag_db_dir", "rag_db")
        self._storage = None
        if RagStorage is not None:
            try:
                self._storage = RagStorage(db_dir=self.db_dir)
            except Exception as e:
                print(f"[RAG] Storage not available: {e}")
                self._storage = None
        if self._storage is None:
            print("[RAG] Hint: No persistent storage—entries remain only in RAM.")
        self._matrix_mode = (
            bool(self.config.get("rag_matrix_mode", True))
            and np is not None
            and self._storage is not None)
        self._rag_arrays = {}
        self._entry_by_id = {}
        model_cfg = str(self.config.get("rag_embedding_model", "st") or "st")
        self._embedder_tag = model_cfg.rstrip("/").split("/")[-1] or "st"
        self._meta = None
        self._load()
        if self.enabled and bool(self.config.get("rag_index_startup", True)):
            try:
                self.index_knowledge_folder(verbose=False)
            except Exception:
                pass

    def _get_meta(self, config: dict):
        # Cache-Key aus URL+Model, damit unterschiedliche Routings separate Clients bekommen
        url = str(config.get("meta_model_url", "") or "")
        model = str(config.get("meta_model_name", "") or "")
        cache_key = f"{url}|{model}"

        # Alte Struktur-Migration: self._meta kann noch ein einzelner Client sein
        if not hasattr(self, "_meta_clients"):
            old = getattr(self, "_meta", None)
            self._meta_clients = {}
            if old is not None:
                # Heuristik: altes Modell unter einem Default-Key ablegen
                self._meta_clients["_legacy"] = old

        client = self._meta_clients.get(cache_key)
        if client is None:
            try:
                from .background_agent import MetaModelClient
            except ImportError:
                return None
            try:
                client = MetaModelClient(config)
            except Exception:
                return None
            self._meta_clients[cache_key] = client

        return client if getattr(client, "available", False) else None

    def _load(self):
        if getattr(self, "_storage", None) is None:
            self.entries = []
            self._entry_by_id = {}
            return

        try:
            if getattr(self, "_matrix_mode", False) and hasattr(self._storage, "load_full"):
                entries, arrays = self._storage.load_full()
                self.entries = entries
                self._rag_arrays = {}
                for arr in arrays:
                    embedder = str(arr.get("embedder", "st"))
                    matrix = arr.get("matrix")
                    ids = [str(x) for x in arr.get("ids", [])]
                    if matrix is None or not ids:
                        continue
                    dim = int(arr.get("dim") or (matrix.shape[1] if len(matrix.shape) > 1 else 0))
                    self._rag_arrays[(embedder, dim)] = {
                        "ids": ids,
                        "matrix": matrix,
                        "id_to_row": {eid: i for i, eid in enumerate(ids)}}
                self._rebuild_entry_index()
            else:
                self.entries = self._storage.load()
                self._rebuild_entry_index()
        except Exception as e:
            print(f"[RAG] Error while loading {self.db_dir}: {e}")
            self.entries = []
            self._entry_by_id = {}

    def _rebuild_entry_index(self):
        self._entry_by_id = {
            str(e.get("id", "")): e
            for e in self.entries
            if e.get("id")}

    def _arrays_as_list(self) -> List[Dict[str, Any]]:
        out = []
        for (embedder, dim), arr in self._rag_arrays.items():
            ids = arr.get("ids") or []
            matrix = arr.get("matrix")
            if not ids or matrix is None:
                continue
            out.append({
                "embedder": embedder,
                "dim": dim,
                "ids": ids,
                "matrix": matrix})
        return out

    def _sync_arrays_to_entries(self):
        valid_ids = set(self._entry_by_id.keys())
        for key in list(self._rag_arrays.keys()):
            arr = self._rag_arrays[key]
            ids = arr.get("ids") or []
            matrix = arr.get("matrix")
            if matrix is None:
                del self._rag_arrays[key]
                continue
            keep = [
                i for i, eid in enumerate(ids)
                if str(eid) in valid_ids]
            if len(keep) == len(ids):
                if "id_to_row" not in arr:
                    arr["id_to_row"] = {
                        eid: i for i, eid in enumerate(ids)}
                continue
            if not keep:
                del self._rag_arrays[key]
                continue
            new_ids = [ids[i] for i in keep]
            new_matrix = matrix[keep]
            arr["ids"] = new_ids
            arr["matrix"] = new_matrix
            arr["id_to_row"] = {
                eid: i for i, eid in enumerate(new_ids)}

    def _add_vectors_to_arrays(self, new_vectors):
        if np is None:
            return
        groups = {}
        for eid, embedder, vec in new_vectors:
            if not eid or not vec:
                continue
            key = (str(embedder), len(vec))
            if key not in groups:
                groups[key] = {"ids": [], "vecs": []}
            groups[key]["ids"].append(str(eid))
            groups[key]["vecs"].append(vec)
        for key, group in groups.items():
            new_ids = group["ids"]
            new_matrix = np.asarray(group["vecs"], dtype=np.float32)
            if new_matrix.ndim == 1:
                new_matrix = new_matrix.reshape(1, -1)
            arr = self._rag_arrays.get(key)
            if arr is None or arr.get("matrix") is None or len(arr.get("ids", [])) == 0:
                self._rag_arrays[key] = {
                    "ids": new_ids,
                    "matrix": new_matrix,
                    "id_to_row": {
                        eid: i for i, eid in enumerate(new_ids)}}
            else:
                if arr["matrix"].ndim == 1:
                    arr["matrix"] = arr["matrix"].reshape(1, -1)
                start = len(arr["ids"])
                arr["ids"].extend(new_ids)
                arr["matrix"] = np.vstack([arr["matrix"], new_matrix])
                if "id_to_row" not in arr:
                    arr["id_to_row"] = {
                        eid: i for i, eid in enumerate(arr["ids"][:start])}
                for j, eid in enumerate(new_ids):
                    arr["id_to_row"][eid] = start + j

    def _save(self):
        for e in self.entries:
            try:
                self._normalize_entry(e)
            except Exception:
                pass
        self._rebuild_entry_index()
        if getattr(self, "_matrix_mode", False):
            self._sync_arrays_to_entries()
        if getattr(self, "_storage", None) is None:
            print("[RAG] No storage available – Save skipped (entries in RAM only).")
            return
        try:
            if getattr(self, "_matrix_mode", False) and hasattr(self._storage, "save_all"):
                self._storage.save_all(self.entries, arrays=self._arrays_as_list())
            else:
                self._storage.save_all(self.entries)
        except Exception as e:
            print(f"[RAG] Error saving to {self.db_dir}: {e}")

    def _save_meta(self):
        for e in self.entries:
            try:
                self._normalize_entry(e)
            except Exception:
                pass
        if getattr(self, "_storage", None) is not None:
            try:
                self._storage.save_meta(self.entries)
                return
            except Exception as e:
                print(f"[RAG] Error saving meta data in {self.db_dir}: {e}. Fallback: full save.")
        self._save()


    def _resolve_embedding_model(self) -> str:
        model_name = str(
            self.config.get("rag_embedding_model", "sentence-transformers/all-MiniLM-L6-v2") or "sentence-transformers/all-MiniLM-L6-v2").strip()
        marker_files = ("config.json", "modules.json", "config_sentence_transformers.json", "model.safetensors", "pytorch_model.bin",)
        def _is_valid_model_dir(path: str) -> bool:
            if not os.path.isdir(path):
                return False
            return any(os.path.exists(os.path.join(path, f)) for f in marker_files)
        def _set_offline():
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
        if os.path.isabs(model_name) and _is_valid_model_dir(model_name):
            _set_offline()
            return model_name
        project_path = p(model_name)
        if _is_valid_model_dir(project_path):
            _set_offline()
            return project_path
        local_base = cfg_path(self.config, "rag_local_model_dir", "models")
        folder_name = model_name.split("/")[-1] if "/" in model_name else model_name
        local_dir = os.path.join(local_base, folder_name)
        if _is_valid_model_dir(local_dir):
            _set_offline()
            return local_dir
        return model_name

    def _chunks_knowledge(self, text: str, max_chars: int, overlap: int,
                          use_headings: bool = True) -> List[Dict[str, str]]:
        text = (text or "").strip()
        if not text:
            return []
        blocks = []
        current_heading = ""
        buf = ""

        def flush():
            nonlocal buf
            if buf.strip():
                blocks.append({"heading": current_heading, "text": buf.strip()})
            buf = ""

        for line in text.splitlines():
            stripped = line.strip()
            if use_headings and stripped.startswith("#") and 1 < len(stripped) < 120:
                flush()
                current_heading = stripped.lstrip("#").strip() or current_heading
            elif stripped == "":
                flush()
            else:
                buf += line + "\n"
        flush()
        pieces = []
        for block in blocks:
            para = block["text"]
            heading = block["heading"]
            if len(para) <= max_chars:
                pieces.append({"heading": heading, "text": para})
                continue
            start = 0
            while start < len(para):
                end = min(start + max_chars, len(para))
                if end < len(para):
                    brk = max(para.rfind("\n", start, end), para.rfind(". ", start, end))
                    if brk > start + max_chars // 2:
                        end = brk + 1
                pieces.append({"heading": heading, "text": para[start:end].strip()})
                if end >= len(para):
                    break
                start = max(start + 1, end - overlap)
        merged = []
        for piece in pieces:
            if (merged and len(merged[-1]["text"]) < 200
                    and len(merged[-1]["text"]) + len(piece["text"]) + 2 <= max_chars):
                merged[-1]["text"] = (merged[-1]["text"] + "\n\n" + piece["text"]).strip()
            else:
                merged.append(dict(piece))
        return merged

    def _meta_refine_chunks(self, chunks: List[Dict[str, str]], max_chars: int) -> List[Dict[str, str]]:
        if len(chunks) < 2:
            return chunks
        try:
            from .background_agent import MetaModelClient
        except ImportError:
            return chunks
        meta = MetaModelClient(self.config)
        if not meta.available:
            return chunks
        listing = ""
        for i, ch in enumerate(chunks):
            listing += f"--- CHUNK {i} ---\n{ch['text'][:400]}\n"
        prompt = (
            "These are consecutive chunks of one document. Some logical blocks were "
            "split across chunk boundaries.\n"
            "Identify groups of CONSECUTIVE chunks that belong to one logical block "
            "and should be merged.\n\n"
            f"{listing}\n"
            "Answer ONLY with JSON: {\"merge\": [[0,1],[3,4]]} or {\"merge\": []}")
        response = meta.chat(prompt, max_tokens=200, temperature=0.0)
        if not response:
            return chunks
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            groups = json.loads(response[start:end]).get("merge", [])
        except Exception:
            return chunks
        if not groups or not isinstance(groups, list):
            return chunks
        merge_targets = {}
        for group in groups:
            if not isinstance(group, list) or len(group) < 2:
                continue
            idxs = sorted({int(i) for i in group
                           if isinstance(i, int) and 0 <= i < len(chunks)})
            idxs = [x for i, x in enumerate(idxs) if i == 0 or x == idxs[i - 1] + 1]
            if len(idxs) >= 2:
                merge_targets.setdefault(idxs[0], []).extend(idxs[1:])

        result = []
        handled = set()
        cap = int(max_chars * 1.4)
        for i, ch in enumerate(chunks):
            if i in handled:
                continue
            if i in merge_targets:
                idxs = sorted([i] + merge_targets[i])
                text = "\n\n".join(chunks[j]["text"] for j in idxs)
                if len(text) <= cap:
                    result.append({"heading": ch.get("heading", ""), "text": text})
                    handled.update(idxs)
                    continue
            result.append(dict(ch))
            handled.add(i)
        return result

    def _embed(self, text: str) -> List[float]:
        text = (text or "").strip()
        if not text:
            return []
        if not self._st_failed:
            try:
                if self._model is None:
                    with self._model_lock:
                        if self._model is None:
                            from sentence_transformers import SentenceTransformer
                            model_name = self._resolve_embedding_model()
                            try:
                                import torch
                                torch.set_num_threads(4)
                            except Exception:
                                pass
                            self._model = SentenceTransformer(
                                model_name,
                                device="cpu",
                                trust_remote_code=True)
                vec = self._model.encode([text], normalize_embeddings=True)[0]
                return [float(x) for x in vec]
            except Exception as e:
                print('[RAG] Embedding model failed:', repr(e))
                self._st_failed = True
        return []

    def preload_embedding_model(self) -> Dict[str, Any]:
        try:
            if self._st_failed:
                return {"success": False, "mode": "none"}
            if self._model is None:
                with self._model_lock:
                    if self._model is None:
                        from sentence_transformers import SentenceTransformer
                        model_name = self._resolve_embedding_model()
                        try:
                            import torch
                            torch.set_num_threads(4)
                        except Exception:
                            pass
                        self._model = SentenceTransformer(
                            model_name,
                            device="cpu",
                            trust_remote_code=True)
            self._model.encode(["Vishva RAG Warmup"], normalize_embeddings=True)
            self._preload_done = True
            return {
                "success": True,
                "mode": "st",
                "model": self._resolve_embedding_model()}
        except Exception as e:
            print("[RAG] Preload embedding model failed:", repr(e))
            self._st_failed = True
            return {
                "success": False,
                "error": str(e),
                "mode": "none"}

    def _cosine(self, a: List[float], b: List[float]) -> float:
        if not a or not b:
            return 0.0
        n = min(len(a), len(b))
        dot = sum(a[i] * b[i] for i in range(n))
        na = math.sqrt(sum(a[i] * a[i] for i in range(n)))
        nb = math.sqrt(sum(b[i] * b[i] for i in range(n)))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def _chunks(self, text: str, max_chars: Optional[int] = None, overlap: Optional[int] = None) -> List[str]:
        text = (text or "").strip()
        if not text:
            return []
        max_chars = max_chars or int(self.config.get("rag_chunk_max_chars", 1000))
        overlap = overlap if overlap is not None else int(self.config.get("rag_chunk_overlap_chars", 150))
        overlap = max(0, min(overlap, max_chars // 2))
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        pieces = []
        for para in paragraphs:
            if len(para) <= max_chars:
                pieces.append(para)
                continue
            start = 0
            while start < len(para):
                end = min(start + max_chars, len(para))
                brk = max(para.rfind("\n", start, end), para.rfind(". ", start, end))
                if brk > start + max_chars // 2:
                    end = brk + 1
                pieces.append(para[start:end])
                if end >= len(para):
                    break
                start = max(start + 1, end - overlap)
        chunks = []
        current = ""
        for piece in pieces:
            candidate = (current + "\n\n" + piece).strip() if current else piece
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = piece
        if current:
            chunks.append(current)
        return chunks

    def _entry_id(self, source: str, text: str) -> str:
        raw = f"{source}|{text}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:20]

    def normalize_category(self, category: str) -> str:
        c = str(category or "").strip().lower().replace("-", "_").replace(" ", "_")
        canonical = [str(x).lower() for x in self.config.get("rag_categories", [])]
        reserved = [str(x).lower() for x in self.config.get("rag_reserved_categories", ["essential", "knowledge"])]
        if c in canonical or c in reserved:
            return c
        return str(self.config.get("rag_category_fallback", "miscellaneous")).lower()

    def add_text(self, text: str, source: str = "manual", category: str = "knowledge",
                metadata: Optional[Dict[str, Any]] = None, save: bool = True,
                max_chars: Optional[int] = None, overlap: Optional[int] = None,
                pre_chunked: bool = False) -> Dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"success": False, "error": "text is empty", "added": 0}
        added = 0
        new_vectors = []
        matrix_mode = getattr(self, "_matrix_mode", False)
        chunk_list = ([text] if pre_chunked
                    else self._chunks(text, max_chars=max_chars, overlap=overlap))
        for chunk in chunk_list:
            entry_id = self._entry_id(source, chunk)
            if str(entry_id) in self._entry_by_id:
                continue
            vec = self._embed(chunk)
            if not vec:
                continue
            embedder = self._embedder_tag
            entry = {
                "id": entry_id,
                "source": source,
                "category": category,
                "metadata": metadata or {},
                "text": chunk,
                "embedder": embedder}
            if isinstance(metadata, dict) and "priority" in metadata:
                try:
                    entry["priority"] = max(0, min(3, int(metadata["priority"])))
                except Exception:
                    pass
            if not matrix_mode:
                entry["embedding"] = vec
            self.entries.append(entry)
            self._entry_by_id[str(entry_id)] = entry
            if vec:
                new_vectors.append((entry_id, embedder, vec))
            added += 1
        if matrix_mode and new_vectors:
            self._add_vectors_to_arrays(new_vectors)
        if save and added:
            self._save()
        try:
            self._rotate_if_needed()
        except Exception:
            pass
        return {"success": True, "added": added}

    def index_knowledge_folder(self, verbose: bool = True) -> Dict[str, Any]:
        if not os.path.exists(self.knowledge_dir):
            os.makedirs(self.knowledge_dir, exist_ok=True)
        allowed = {".md", ".txt", ".json", ".py", ".csv", ".yaml", ".yml"}
        k_max = int(self.config.get("rag_knowledge_max_chars", 1200))
        k_overlap = int(self.config.get("rag_knowledge_overlap_chars", 120))
        refine = bool(self.config.get("rag_knowledge_meta_refine", True))
        seen_files = set()
        added_total = 0
        removed_stale = 0
        skipped_unchanged = 0
        for root, _, files in os.walk(self.knowledge_dir):
            for filename in sorted(files):
                ext = os.path.splitext(filename)[1].lower()
                if ext not in allowed:
                    continue
                if filename == "user_messages.md" or filename.endswith(".old"):
                    continue
                path = os.path.join(root, filename)
                seen_files.add(path)
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                except Exception:
                    continue
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
                existing = [e for e in self.entries if e.get("source") == path]
                if existing and all(
                        str((e.get("metadata", {}) or {}).get("file_hash", "")) == content_hash
                        for e in existing):
                    skipped_unchanged += 1
                    continue
                before = len(self.entries)
                self.entries = [e for e in self.entries if e.get("source") != path]
                removed_stale += before - len(self.entries)
                self._rebuild_entry_index()
                chunks = self._chunks_knowledge(
                    content, k_max, k_overlap,
                    use_headings=ext in (".md", ".txt"))
                if not chunks:
                    continue
                if refine:
                    try:
                        chunks = self._meta_refine_chunks(chunks, k_max) or chunks
                    except Exception:
                        pass
                doc_title = os.path.splitext(filename)[0]
                chunk_entries = []
                for i, ch in enumerate(chunks):
                    heading = ch.get("heading", "") or doc_title
                    prefix = f"[{doc_title} › {heading}]" if heading else f"[{doc_title}]"
                    etext = (prefix + "\n" + ch["text"]).strip()
                    res = self.add_text(
                        etext, source=path, category="knowledge",
                        metadata={"file": path, "file_hash": content_hash, "chunk_index": i},
                        save=False, pre_chunked=True)
                    if res.get("success") and res.get("added"):
                        added_total += res["added"]
                        entry = self._entry_by_id.get(self._entry_id(path, etext))
                        if entry:
                            chunk_entries.append(entry)
                for i, entry in enumerate(chunk_entries):
                    md = entry.setdefault("metadata", {})
                    if i > 0:
                        md["prev_id"] = chunk_entries[i - 1].get("id", "")
                    if i < len(chunk_entries) - 1:
                        md["next_id"] = chunk_entries[i + 1].get("id", "")
        before = len(self.entries)
        self.entries = [
            e for e in self.entries
            if not str(e.get("source", "")).startswith(self.knowledge_dir)
            or e.get("source") in seen_files]
        removed_orphans = before - len(self.entries)
        self._rebuild_entry_index()
        self._save()
        if verbose:
            print(f"RAG indexed {added_total} new chunks "
                  f"({removed_stale} stale, {removed_orphans} orphans removed, "
                  f"{skipped_unchanged} unchanged) from {self.knowledge_dir}/")
        try:
            self._rotate_if_needed()
            self._maybe_save_access(True)
        except Exception:
            pass
        return {"success": True, "added_chunks": added_total,
                "removed_stale": removed_stale, "removed_orphans": removed_orphans,
                "skipped_unchanged": skipped_unchanged}

    def search(self, query: str, top_k: Optional[int] = None, min_score: float = 0.0) -> Dict[str, Any]:
        self._score_cache_key = None
        if not self.enabled:
            return {"success": False, "error": "RAG disabled", "results": []}
        query = (query or "").strip()
        if not query:
            return {"success": False, "error": "query is empty", "results": []}
        top_k = top_k or self.top_k
        try:
            min_score = float(min_score or 0.0)
        except Exception:
            min_score = 0.0
        if min_score > 1.0:
            min_score = min_score / 100.0
        qv = self._embed(query)
        if not qv:
            return {"success": True, "query": query, "results": []}
        current_embedder = self._embedder_tag
        scored = self._score_all(qv, current_embedder)
        if min_score > 0:
            scored = [
                (score, entry)
                for score, entry in scored
                if score >= min_score]
        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, entry in scored[:top_k]:
            results.append({
                "id": entry.get("id", ""),
                "score": round(float(score), 4),
                "source": entry.get("source", ""),
                "category": entry.get("category", ""),
                "text": entry.get("text", "")})
        try:
            accessed_ids = self._track_access(results) or set()
            self._cycle_tick(accessed_ids)
            self._rotate_if_needed()
            self._maybe_save_access(False)
        except Exception:
            pass
        return {"success": True, "query": query, "results": results}

    def query_no_track(self, query: str, top_k: int = 0, min_score: float = 0.0) -> Dict[str, Any]:
        if not self.enabled:
            return {"success": False, "error": "RAG disabled", "results": []}
        query = (query or "").strip()
        if not query:
            return {"success": False, "error": "query is empty", "results": []}
        qv = self._embed(query)
        if not qv:
            return {"success": True, "query": query, "results": [], "total_matches": 0}
        current_embedder = self._embedder_tag
        scored = self._score_all(qv, current_embedder)
        scored = [
            (float(score), entry)
            for score, entry in scored
            if score >= min_score]
        scored.sort(key=lambda x: x[0], reverse=True)
        total_matches = len(scored)
        if top_k and top_k > 0:
            scored = scored[:top_k]
        results = []
        for score, entry in scored:
            results.append({
                "id": entry.get("id", ""),
                "score": round(float(score), 4),
                "source": entry.get("source", ""),
                "category": entry.get("category", ""),
                "text": entry.get("text", ""),
                "access_count": int(entry.get("access_count", 0) or 0),
                "age_cycles": int(entry.get("age_cycles", 0) or 0),
                "created_at": entry.get("created_at", ""),
                "last_access": entry.get("last_access", "")})
        return {
            "success": True,
            "query": query,
            "mode": current_embedder,
            "total_matches": total_matches,
            "results": results}

    def _score_all(self, qv, current_embedder):
        entries = self.entries
        dim = len(qv)
        if getattr(self, "_matrix_mode", False) and np is not None:
            try:
                arr = self._rag_arrays.get((current_embedder, dim))
                if arr is None or arr.get("matrix") is None or not arr.get("ids"):
                    return []
                matrix = arr["matrix"]
                ids = arr["ids"]
                if matrix.ndim == 1:
                    matrix = matrix.reshape(1, -1)
                qv_np = np.asarray(qv, dtype=np.float32)
                scores = matrix @ qv_np
                out = []
                for j, eid in enumerate(ids):
                    entry = self._entry_by_id.get(str(eid))
                    if entry is not None:
                        out.append((float(scores[j]), entry))
                return out
            except Exception as e:
                print(f"[RAG] matrix scoring error: {type(e).__name__}: {e}")

        if np is not None:
            try:
                cache_key = (current_embedder, len(qv), len(entries))
                if getattr(self, "_score_cache_key", None) != cache_key:
                    eligible = [
                        i for i, e in enumerate(entries)
                        if e.get("embedder", "st") == current_embedder
                        and len(e.get("embedding") or []) == len(qv)]
                    if eligible:
                        matrix = np.asarray(
                            [entries[i]["embedding"] for i in eligible],
                            dtype=np.float32)
                    else:
                        matrix = np.zeros((0, len(qv)), dtype=np.float32)
                    self._score_cache = (eligible, matrix)
                    self._score_cache_key = cache_key
                eligible, matrix = self._score_cache
                if not eligible:
                    return []
                qv_np = np.asarray(qv, dtype=np.float32)
                scores = matrix @ qv_np
                return [(float(scores[j]), entries[i]) for j, i in enumerate(eligible)]
            except Exception as e:
                print(f"[RAG] numpy fallback scoring error: {type(e).__name__}: {e}")
        out = []
        for entry in entries:
            if entry.get("embedder", "st") != current_embedder:
                continue
            emb = entry.get("embedding") or []
            if len(emb) != len(qv):
                continue
            out.append((self._cosine(qv, emb), entry))
        return out

    def is_near_duplicate(self, text: str, category: str = "", threshold: float = 0.90, max_compare: int = 300) -> bool:
        if not text:
            return False
        cand = self._embed(text)
        if not cand:
            return False
        current = self._embedder_tag
        dim = len(cand)
        if getattr(self, "_matrix_mode", False):
            try:
                arr = self._rag_arrays.get((current, dim))
                if not arr or not arr.get("id_to_row") or arr.get("matrix") is None:
                    return False
                id_to_row = arr["id_to_row"]
                matrix = arr["matrix"]
                cand_np = np.asarray(cand, dtype=np.float32)
                checked = 0
                for entry in reversed(self.entries):
                    if category and entry.get("category") != category:
                        continue
                    eid = str(entry.get("id", ""))
                    row = id_to_row.get(eid)
                    if row is None:
                        continue
                    try:
                        score = float(np.dot(cand_np, matrix[row]))
                    except Exception:
                        score = self._cosine(cand, matrix[row].tolist())
                    if score >= threshold:
                        return True
                    checked += 1
                    if checked >= max_compare:
                        break
                return False
            except Exception:
                pass
        if category:
            same_cat = [
                e for e in self.entries
                if e.get("category") == category
                and e.get("embedder", "st") == current][-max_compare:]
        else:
            same_cat = [
                e for e in self.entries
                if e.get("embedder", "st") == current][-max_compare:]
        for e in reversed(same_cat):
            emb = e.get("embedding") or []
            if len(emb) != len(cand):
                continue
            if self._cosine(cand, emb) >= threshold:
                return True
        return False

    def format_essential_context(self) -> str:
        if not self.enabled:
            return ""
        debug_injection = bool(self.config.get("rag_debug_injection", False))
        essential_categories = self.config.get("rag_essential_categories", ["essential"])
        if not isinstance(essential_categories, list):
            essential_categories = ["essential"]
        essential_categories = [str(c).strip() for c in essential_categories if str(c).strip()]
        if not essential_categories:
            return ""
        lines = ["[ESSENTIAL_CONTEXT]"]
        injected_count = 0
        max_chars = int(self.config.get("rag_max_context_chars", 2500))
        used = len(lines[0])
        for entry in self.entries:
            if entry.get("category", "") not in essential_categories:
                continue
            raw = str(entry.get("text", ""))
            text = " ".join(raw.split())
            if not text:
                continue
            line = f"- [{entry.get('source', '?')}] {text}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
            injected_count += 1
        if debug_injection:
            print(f"\n[RAG-DEBUG] ══════════════════════════════════════════")
            print(f"[RAG-DEBUG] Essential-Injektion: {injected_count} Einträge")
            for line in lines[1:]:
                print(f"[RAG-DEBUG]   ★ {line[:80]}")
            print(f"[RAG-DEBUG] ══════════════════════════════════════════\n")
        expand_n = int(self.config.get("rag_knowledge_expand_neighbors", 1))
        inject_max = int(self.config.get("rag_inject_max_chars", 2500))
        if expand_n > 0 and len(lines) > 1:
            total_chars = sum(len(l) for l in lines)
            for line in list(lines[1:]):
                if total_chars >= inject_max:
                    break
                # Finde den Eintrag für diese Zeile
                for entry in self.entries:
                    if entry.get("category") == "knowledge" and str(entry.get("text", ""))[:40] in line:
                        meta = entry.get("metadata", {})
                        for key in ("prev_id", "next_id"):
                            neighbor_id = meta.get(key, "")
                            if neighbor_id:
                                neighbor = self._entry_by_id.get(str(neighbor_id))
                                if neighbor:
                                    ntext = str(neighbor.get("text", ""))
                                    nline = f"- [context] {ntext}"
                                    if total_chars + len(nline) <= inject_max:
                                        lines.append(nline)
                                        total_chars += len(nline)
                        break
        return "\n".join(lines) if len(lines) > 1 else ""

    def _normalize_entry(self, entry: Dict[str, Any]):
        from datetime import datetime
        now = datetime.now().isoformat()
        if not entry.get("id"):
            entry["id"] = self._entry_id(
                entry.get("source", ""),
                entry.get("text", ""))
        entry.setdefault("access_count", 0)
        entry.setdefault("age_cycles", 0)
        created = entry.get("created_at") or entry.get("timestamp") or now
        entry.setdefault("created_at", created)
        entry.setdefault("last_access", entry.get("last_access") or created)
        metadata = entry.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
            entry["metadata"] = metadata
        created = entry.get("created_at") or entry.get("timestamp") or now
        entry.setdefault("created_at", created)
        entry.setdefault("last_access", entry.get("last_access") or created)

    def _find_entry_for_result(self, result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(result, dict):
            return None
        rid = result.get("id")
        source = result.get("source", "")
        text = result.get("text", "")
        if not rid:
            rid = self._entry_id(source, text)
        for entry in self.entries:
            if entry.get("id") == rid:
                return entry
        for entry in self.entries:
            if entry.get("source") == source and entry.get("text") == text:
                return entry
        return None

    def _is_protected(self, entry: Dict[str, Any]) -> bool:
        if int(entry.get("priority", 1)) >= 3:
            return True
        cfg = self.config or {}
        protected_sources = cfg.get("rag_rotation_protected_sources", []) or []
        protected_categories = cfg.get("rag_rotation_protected_categories", ["manual", "important"])
        if protected_categories is None:
            protected_categories = []
        cat = str(entry.get("category", ""))
        src = str(entry.get("source", ""))
        if cat in [str(c) for c in protected_categories]:
            return True
        for prefix in protected_sources:
            p = str(prefix)
            if p and src.startswith(p):
                return True
        return False

    def _track_access(self, results: List[Dict[str, Any]]) -> set:
        from datetime import datetime
        cfg = self.config or {}
        accessed_ids = set()
        if not bool(cfg.get("rag_access_tracking_enabled", True)):
            return accessed_ids
        if not results:
            return accessed_ids
        now = datetime.now().isoformat()
        for result in results:
            entry = self._find_entry_for_result(result)
            if not entry:
                continue
            self._normalize_entry(entry)
            entry["access_count"] = int(entry.get("access_count", 0)) + 1
            entry["last_access"] = now
            if entry.get("id"):
                accessed_ids.add(entry.get("id"))
        return accessed_ids

    def _cycle_tick(self, accessed_ids: Optional[set] = None) -> bool:
        cfg = self.config or {}
        if not bool(cfg.get("rag_cycle_aging_enabled", True)):
            return False
        if not self.entries:
            return False
        accessed_ids = accessed_ids or set()
        changed = False
        for entry in self.entries:
            self._normalize_entry(entry)
            eid = entry.get("id")
            if eid and eid in accessed_ids:
                if int(entry.get("age_cycles", 0)) != 0:
                    entry["age_cycles"] = 0
                    changed = True
            else:
                entry["age_cycles"] = int(entry.get("age_cycles", 0)) + 1
                changed = True
        return changed

    def _rotate_if_needed(self) -> bool:
        if not self.entries:
            return False
        cfg = self.config or {}
        def _as_int(value, default):
            try:
                return int(value)
            except Exception:
                return default
        max_entries = _as_int(cfg.get("rag_max_entries", 5000), 5000)
        unused_max_cycles = _as_int(cfg.get("rag_unused_max_cycles", 100), 100)
        unused_threshold = _as_int(cfg.get("rag_unused_access_threshold", 0), 0)
        low_access_max_cycles = _as_int(cfg.get("rag_low_access_max_cycles", 200), 200)
        low_access_threshold = _as_int(cfg.get("rag_low_access_threshold", 2), 2)
        hard_max = bool(cfg.get("rag_rotation_hard_max", False))
        for entry in self.entries:
            self._normalize_entry(entry)
        before = len(self.entries)
        if unused_max_cycles > 0:
            self.entries = [
                e for e in self.entries
                if self._is_protected(e)
                or not (
                    int(e.get("access_count", 0)) <= unused_threshold
                    and int(e.get("age_cycles", 0)) >= unused_max_cycles)]
        if low_access_max_cycles > 0:
            self.entries = [
                e for e in self.entries
                if self._is_protected(e)
                or not (
                    int(e.get("access_count", 0)) <= low_access_threshold
                    and int(e.get("age_cycles", 0)) >= low_access_max_cycles)]
        if len(self.entries) > max_entries:
            removable = [e for e in self.entries if not self._is_protected(e)]
            need = len(self.entries) - max_entries
            removable.sort(key=lambda e: (
                int(e.get("access_count", 0)),
                -int(e.get("age_cycles", 0)),
                str(e.get("last_access") or e.get("created_at") or "")))
            remove_ids = {e.get("id") for e in removable[:need]}
            self.entries = [e for e in self.entries if e.get("id") not in remove_ids]

        if hard_max and len(self.entries) > max_entries:
            all_sorted = sorted(
                self.entries,
                key=lambda e: str(e.get("last_access") or e.get("created_at") or ""))
            remove_extra = len(self.entries) - max_entries
            remove_ids = {e.get("id") for e in all_sorted[:remove_extra]}
            self.entries = [e for e in self.entries if e.get("id") not in remove_ids]

        if len(self.entries) != before:
            try:
                self._rebuild_entry_index()
                self._save()
            except Exception:
                pass
            return True
        return False

    def _maybe_save_access(self, force: bool = False):
        cfg = self.config or {}
        try:
            interval = int(cfg.get("rag_access_save_interval", 20) or 20)
        except Exception:
            interval = 20
        counter = getattr(self, "_access_save_counter", 0) + 1
        self._access_save_counter = counter
        if force or interval <= 1 or counter >= interval:
            try:
                self._save_meta()
            except Exception:
                pass
            self._access_save_counter = 0

    def find_similar_entries(self, text: str, threshold: float = 0.92, max_results: int = 10) -> List[Dict[str, Any]]:
        if not text or not self.enabled:
            return []
        qv = self._embed(text)
        if not qv:
            return []
        current_embedder = self._embedder_tag
        scored = self._score_all(qv, current_embedder)
        results = []
        for score, entry in scored:
            if score >= threshold:
                results.append({
                    "id": entry.get("id", ""),
                    "score": round(float(score), 4),
                    "text": entry.get("text", ""),
                    "priority": int(entry.get("priority", 1)),
                    "access_count": int(entry.get("access_count", 0)),
                    "category": entry.get("category", ""),
                    "source": entry.get("source", ""),
                    "created_at": entry.get("created_at", ""),
                    "maintained": entry.get("maintained", False),})
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:max_results]

    def enrich_context(self, user_message: str, config: dict, prev_user_msg: str = "", prev_asst_msg: str = "") -> str:
        if not self.enabled:
            return ""
        if not bool(config.get("rag_meta_enrichment_enabled", True)):
            return ""
        if len((user_message or "").strip()) < 8:
            return ""
        debug_rag = bool(config.get("rag_debug_injection", False))
        meta = self._get_meta(config)
        if meta is None:
            if debug_rag:
                print("[RAG-ENRICH] Meta model not available")
            return ""
        context_parts = []
        if prev_user_msg:
            context_parts.append(f"[PREVIOUS TURN — USER]: {prev_user_msg[:200]}")
        if prev_asst_msg:
            context_parts.append(f"[PREVIOUS TURN — ASSISTANT]: {prev_asst_msg[:200]}")
        context_parts.append(f"[CURRENT MESSAGE — USER]: {user_message[:300]}")
        full_context = "\n".join(context_parts)

        search_prompt = (
            "You decide whether stored LONG-TERM MEMORY should be retrieved to answer this message accurately and personally.\n\n"
            f"{full_context}\n\n"
            "The memory contains these categories:\n"
            "- user_info: name, profession, skills, relationships, pets, important dates, facts about the user\n"
            "- preference: likes/dislikes, hobbies, communication style, habits, daily rhythm, frameworks\n"
            "- system: hardware, OS, paths, services, devices, network\n"
            "- agent: assistant's identity, behavioral rules, instructions, taboos\n"
            "- knowhow: general knowledge, how-tos, references, documentation\n"
            "- project: current project facts, technical details, decisions\n"
            "- miscellaneous: other facts that don't fit elsewhere\n\n"
            "DEFAULT: Answer YES and generate search queries.\n\n"
            "Answer NO ONLY for:\n"
            "- Pure greetings without a question ('hello', 'hi', 'moin', 'good morning', 'hey there')\n"
            "- Pure thanks/acknowledgments without a question ('thanks', 'thx', 'ok', 'got it', 'alright')\n"
            "- Pure test messages ('test', 'ping', '123', 'foo', 'bar')\n"
            "- Single words without context\n"
            "- Pure farewells ('bye', 'goodbye', 'see you', 'cya')\n\n"
            "Answer YES for everything else, especially:\n"
            "- Questions about the user, the assistant, or their system\n"
            "- Requests that benefit from personalization (recommendations, gifts, food, books, music, planning)\n"
            "- Messages referencing shared knowledge ('as usual', 'my regular setup', 'I told you before', 'wie besprochen', 'wie vorhin')\n"
            "- Messages that CONTINUE a previous turn (the PREVIOUS TURN provides context for references like 'that', 'it', 'the project', 'wie gesagt')\n"
            "- Questions about PEOPLE (names, relationships, jobs, facts about someone)\n"
            "- Questions about a PROJECT, TECHNOLOGY, or HOW-TO you've discussed before\n"
            "- Questions about the user's OWN CODE, FILES, or PATHS\n"
            "- Any substantive question that could benefit from past context\n\n"
            "If YES: generate 2-5 SEARCH QUERIES in GERMAN. "
            "Queries must be FULL PHRASES or QUESTIONS, not just keywords.\n"
            "Examples:\n"
            "- 'was weißt du über Karlheinz Müller?' → 'wer ist karlheinz müller' | 'karlheinz müller beruf'\n"
            "- 'welche GPU habe ich?' → 'welche grafikkarte hat der user' | 'system hardware gpu'\n"
            "- 'wie sollst du mich nennen?' → 'name des users' | 'wie der user genannt werden will'\n"
            "- 'mein übliches Setup' → 'das übliche setup des users' | 'bevorzugte arbeitsumgebung'\n"
            "- 'wir hatten doch über FastAPI geredet' → 'fastapi projekt' | 'fastapi entscheidungen'\n"
            "- 'was waren meine Hobbys?' → 'hobbys des users' | 'freizeitaktivitäten vorlieben'\n\n"
            "Format: YES|query1|query2|query3\nOr: NO")
        response1 = meta.chat(search_prompt, max_tokens=80, temperature=0.0)
        if debug_rag:
            print(f"[RAG-ENRICH] Stage 1 response: {response1}")
        if not response1 or response1.upper().startswith("NO"):
            if debug_rag:
                print("[RAG-ENRICH] No RAG context needed")
            return ""
        parts = response1.split("|")
        queries = [p.strip() for p in parts[1:] if p.strip()]
        if not queries:
            queries = [q.strip() for q in response1.split("\n")
                       if q.strip() and not q.strip().upper().startswith("YES")]
        if not queries:
            if debug_rag:
                print("[RAG-ENRICH] No queries parsed")
            return ""
        max_queries = int(config.get("rag_meta_max_search_queries", 3))
        queries = queries[:max_queries]
        if debug_rag:
            print(f"[RAG-ENRICH] Stage 1 queries: {queries}")
        all_results = []
        seen_ids = set()
        for query in queries:
            res = self.query_no_track(query, top_k=5, min_score=0.2)
            if not res.get("success"):
                continue
            for r in res.get("results", []):
                rid = r.get("id", "")
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    all_results.append(r)
        if not all_results:
            if debug_rag:
                print("[RAG-ENRICH] No RAG results found")
            return ""
        try:
            access_min = float(config.get("rag_injection_access_min_score", 0.8))
            accessed = [
                r for r in all_results
                if float(r.get("score", 0.0)) >= access_min]
            if accessed:
                accessed_ids = self._track_access(accessed) or set()
                self._cycle_tick(accessed_ids)
                self._rotate_if_needed()
                self._maybe_save_access(False)
        except Exception:
            pass
        if debug_rag:
            print(f"[RAG-ENRICH] Stage 2: {len(all_results)} results found")
            for i, r in enumerate(all_results[:10], 1):
                entry = self._entry_by_id.get(str(r.get("id", "")))
                prio = int(entry.get("priority", 1)) if entry else 1
                print(f"[RAG-ENRICH]   #{i}: prio={prio} score={r.get('score',0):.3f} "
                      f"[{r.get('category','?')}] {r.get('text','')[:60]}")
        expand_n = int(config.get("rag_knowledge_expand_neighbors", 1))
        inject_max = int(config.get("rag_inject_max_chars", 2500))
        results_text = ""
        used_chars = 0
        for i, r in enumerate(all_results[:10], 1):
            entry = self._entry_by_id.get(str(r.get("id", "")))
            priority = int(entry.get("priority", 1)) if entry else 1
            block = f"{i}. [priority={priority}] {r.get('text', '')[:200]}\n"
            if expand_n > 0 and entry and entry.get("category") == "knowledge":
                md = entry.get("metadata", {}) or {}
                for key in ("prev_id", "next_id"):
                    nid = str(md.get(key, "") or "")
                    if not nid:
                        continue
                    neighbor = self._entry_by_id.get(nid)
                    if not neighbor:
                        continue
                    addition = f"   ↳ {str(neighbor.get('text', ''))[:200]}\n"
                    if used_chars + len(addition) <= inject_max:
                        block += addition
                        used_chars += len(addition)
            results_text += block
            used_chars += len(block)
        filter_prompt = (
            f"Conversation context:\n{full_context}\n\n"
            f"Memory entries found by search:\n{results_text}\n\n"
            "Instructions:\n"
            "- Select ONLY entries that clearly help answer or personalize the CURRENT message\n"
            "- If the current message references the PREVIOUS TURN (e.g. 'that', 'it', 'wie vorhin', 'das Projekt'), "
            "prioritize entries that relate to either turn\n"
            "- priority 3 beats lower priorities\n"
            "- Output a compact bullet block, max 120 words, exactly this format:\n"
            "  [MEMORY]\n"
            "  - fact\n"
            "  - fact\n"
            "- If nothing is relevant: NO_RELEVANT_INFO")
        response3 = meta.chat(filter_prompt, max_tokens=300, temperature=0.1)
        if debug_rag:
            print(f"[RAG-ENRICH] Stage 3 response: {response3[:100] if response3 else 'None'}")
        if not response3 or "NO_RELEVANT_INFO" in response3.upper():
            if debug_rag:
                print("[RAG-ENRICH] No relevant info found by meta model")
            return ""
        #else:
        #    print("💉")
        return response3.strip()

    def extract_from_turn(self, user_message: str, assistant_answer: str, config: dict, prev_user_msg: str = "", prev_asst_msg: str = ""):
        if not self.enabled:
            return
        debug_rag = bool(config.get("rag_debug_injection", False))
        try:
            from .background_agent import MetaModelClient
        except ImportError:
            return
        meta = self._get_meta(config)
        if not meta.available:
            return
        context_parts = []
        try:
            res = self.query_no_track(user_message, top_k=5, min_score=0.3)
            existing = [f"- {r.get('text', '')[:120]}" for r in res.get("results", [])]
        except Exception:
            existing = []
        if existing:
            context_parts.append("[ALREADY STORED — do NOT extract these or variants of them]:\n"+"\n".join(existing))
        if prev_user_msg:
            context_parts.append(f"[PREVIOUS TURN — USER]: {prev_user_msg[:200]}")
        if prev_asst_msg:
            context_parts.append(f"[PREVIOUS TURN — ASSISTANT]: {prev_asst_msg[:200]}")
        context_parts.append(f"[CURRENT TURN — USER]: {user_message[:200]}")
        context_parts.append(f"[CURRENT TURN — ASSISTANT]: {assistant_answer[:300]}")
        full_context = "\n".join(context_parts)
        extraction_prompt = (
            f"Analyze this conversation for NEW knowledge worth saving permanently.\n\n"
            f"{full_context}\n\n"
            f"SPEAKER ATTRIBUTION — decide WHO each fact is about:\n"
            f"- Lines labeled '... USER' are spoken BY THE USER.\n"
            f"- Lines labeled '... ASSISTANT' are spoken BY THE ASSISTANT.\n"
            f"- A fact belongs to user_info/preference/system ONLY if it describes the USER.\n"
            f"- When the ASSISTANT talks about ITSELF (its name, capabilities, what IT can do, "
            f"rules it follows), that is 'agent' knowledge — NEVER attribute it to the user.\n"
            f"- If the user ASKS about the assistant (e.g. 'Was kannst du?'), the answer "
            f"describes the ASSISTANT → category 'agent', not user_info.\n"
            f"- The user merely ASKING a question is not a fact about the user.\n\n"
            f"Extract factual, reusable knowledge that is NEW (not already stored).\n\n"
            f"CATEGORIES and their PRIORITY RANGES:\n"
            f"- user_info (priority 2-3): Identity, name, job, skills the USER has, relationships, pets, facts about the user\n"
            f"- preference (priority 2-3): The USER's likes/dislikes, communication style, habits, routines, tools/languages the USER uses\n"
            f"- system (priority 2-3): The USER's hardware, OS, paths, services, devices, network\n"
            f"- agent (priority 2-3): The ASSISTANT's identity, capabilities, behavioral rules, instructions, taboos\n"
            f"- knowhow (priority 1-2): General knowledge, how-tos, references, documentation facts\n"
            f"- project (priority 0-1): Current project facts (temporary, subject to rotation)\n"
            f"- miscellaneous (priority 0-3): Fallback for anything unclear\n\n"
            f"Examples:\n"
            f'- User: "Ich heiße Alex" → {{"text": "Der User heißt Alex", "category": "user_info", "priority": 3}}\n'
            f'- User: "Ich programmiere in Python und nutze FastAPI" → {{"text": "User programmiert in Python, nutzt FastAPI", "category": "preference", "priority": 2}}\n'
            f'- User: "Mein Rechner hat 64GB RAM und eine RTX 4090" → {{"text": "System: 64GB RAM, RTX 4090 GPU", "category": "system", "priority": 2}}\n'
            f'- User: "Du sollst immer auf Deutsch antworten" → {{"text": "Agent soll auf Deutsch antworten", "category": "agent", "priority": 3}}\n'
            f'- Assistant: "Ich kann Python, Bash und Go programmieren" → {{"text": "Agent kann Python, Bash, Go programmieren", "category": "agent", "priority": 2}}  (NOT user_info!)\n'
            f'- Assistant: "Ich bin Vishva, deine Assistentin" → {{"text": "Die Assistentin heißt Vishva", "category": "agent", "priority": 3}}\n'
            f'- User: "Man installiert X mit pip install y" → {{"text": "How-to: X installieren via pip install y", "category": "knowhow", "priority": 2}}\n\n'
            f"Do NOT extract:\n"
            f"- Greetings, smalltalk, questions without concrete answers\n"
            f"- Information the Assistant just retrieved from existing knowledge\n"
            f"- Temporary context that won't be relevant later\n"
            f"- The assistant's capabilities/skills AS IF they were the user's (those are 'agent', not 'user_info')\n\n"
            f"Rules:\n"
            f"- Each entry must be self-contained and name its subject explicitly ('Der User …' or 'Der Agent …')\n"
            f"- Use the priority range specified for each category\n"
            f"- Use ONLY the categories listed above\n\n"
            f"If nothing worth saving, answer: NO_EXTRACT\n"
            f"Otherwise answer with JSON array:\n"
            f'[{{"text": "...", "category": "...", "priority": 0}}]')
        response = meta.chat(extraction_prompt, max_tokens=300, temperature=0.1)
        if debug_rag:
            print(f"[RAG-EXTRACT] Context: prev={bool(prev_user_msg)} | "
                  f"user={user_message[:50]} | asst={assistant_answer[:50]}")
            print(f"[RAG-EXTRACT] Meta response: {response[:100] if response else 'None'}")
        if not response or "NO_EXTRACT" in response.upper():
            return
        try:
            json_start = response.find("[")
            json_end = response.rfind("]") + 1
            if json_start == -1 or json_end == 0:
                return
            entries = json.loads(response[json_start:json_end])
            if not isinstance(entries, list):
                return
        except (json.JSONDecodeError, ValueError):
            if debug_rag:
                print("[RAG-EXTRACT] Could not parse JSON response")
            return
        if not entries:
            return
        max_n = int(self.config.get("rag_meta_max_extraction_entries", 3) or 3)
        ordered = sorted(
            [e for e in entries if isinstance(e, dict)],
            key=lambda e: int(e.get("priority", 0)),
            reverse=True)
        saved = 0
        for entry_data in ordered[:max_n]:
            text = str(entry_data.get("text", "")).strip()
            if not text or len(text) < 10:
                continue
            category = self.normalize_category(entry_data.get("category", ""))
            reserved = {str(x).lower() for x in self.config.get("rag_reserved_categories", ["essential", "knowledge"])}
            if category in reserved:
                if debug_rag:
                    print(f"[RAG-EXTRACT] Reservierte Kategorie '{category}' → Fallback")
                category = str(self.config.get("rag_category_fallback", "miscellaneous")).lower()
            try:
                priority = max(0, min(3, int(entry_data.get("priority", 1))))
            except (ValueError, TypeError):
                priority = 1

            if self.is_duplicate(text, meta, debug_rag):
                if debug_rag:
                    print(f"[RAG-EXTRACT] SKIP (duplicate): {text[:50]}")
                continue
            result = self.add_text(
                text,
                source="conversation",
                category=category,
                metadata={"auto": True, "priority": priority})
            if result.get("success") and result.get("added", 0) > 0:
                saved += 1
                for chunk_text in self._chunks(text):
                    entry_id = self._entry_id("conversation", chunk_text)
                    entry = self._entry_by_id.get(entry_id)
                    if entry:
                        entry["priority"] = priority
                        entry["maintained"] = True
                        from datetime import datetime
                        entry["maintained_at"] = datetime.now().isoformat()
                if debug_rag:
                    print(f"[RAG-EXTRACT] SAVED [prio={priority}] [{category}] {text[:60]}")
        try:
            self._save_meta()
        except Exception:
            pass

    def is_duplicate(self, text: str, meta=None, debug: bool = False) -> bool:
        dedup_threshold = float(self.config.get("rag_dedup_similarity_threshold", 0.90))
        try:
            if self.is_near_duplicate(text, category="", threshold=dedup_threshold):
                return True
        except Exception:
            pass
        try:
            similar = self.find_similar_entries(
                text, threshold=0.55, max_results=5)
        except Exception:
            similar = []
        if not similar:
            return False
        borderline = [s for s in similar if 0.55 <= s.get("score", 0) < dedup_threshold]
        if not borderline:
            return False
        if meta is None:
            try:
                from .background_agent import MetaModelClient
                meta = self._get_meta(config)
                if meta is None:
                    return False
            except ImportError:
                return False
        similar_text = ""
        for s in borderline[:5]:
            similar_text += f"- {s.get('text', '')[:100]}\n"
        dedup_prompt = (
            f"Is the new text semantically a duplicate of any existing text?\n"
            f"Answer only YES or NO.\n\n"
            f"New text: {text[:150]}\n\n"
            f"Existing texts:\n{similar_text}")
        response = meta.chat(dedup_prompt, max_tokens=10, temperature=0.0)
        if debug:
            print(f"[RAG-DEDUP] Meta says: {response}")
        return bool(response and "YES" in response.upper())
