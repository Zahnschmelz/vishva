import os
import re
import json
from typing import List, Dict, Any

try:
    import numpy as np
except ImportError:
    raise RuntimeError("numpy required for RAG-Embedding.")

class RagStorage:
    VERSION = 2
    def __init__(self, db_dir: str = "rag_db"):
        self.db_dir = db_dir
        self.meta_path = os.path.join(db_dir, "meta.json")
        self.manifest_path = os.path.join(db_dir, "manifest.json")
        os.makedirs(db_dir, exist_ok=True)

    def exists(self) -> bool:
        return os.path.exists(self.meta_path)

    def _atomic_write_json(self, path: str, obj: Any, indent=None):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            if indent is not None:
                json.dump(obj, f, ensure_ascii=False, indent=indent)
            else:
                json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)

    def _atomic_write_npy(self, path: str, arr):
        tmp = path + ".tmp.npy"
        np.save(tmp, arr)
        os.replace(tmp, path)

    def load_meta_only(self) -> List[Dict[str, Any]]:
        if not self.exists():
            return []
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                entries = json.load(f)
        except Exception:
            entries = []
        if not isinstance(entries, list):
            return []
        return entries

    def load_arrays(self) -> List[Dict[str, Any]]:
        manifest = {"arrays": []}
        if os.path.exists(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except Exception:
                manifest = {"arrays": []}
        arrays = []
        for arr in manifest.get("arrays", []):
            ids_file = arr.get("ids_file")
            matrix_file = arr.get("matrix_file")
            if not ids_file or not matrix_file:
                continue
            ids_path = os.path.join(self.db_dir, ids_file)
            matrix_path = os.path.join(self.db_dir, matrix_file)
            if not os.path.exists(ids_path) or not os.path.exists(matrix_path):
                continue
            try:
                ids = np.load(ids_path, allow_pickle=True)
                matrix = np.load(matrix_path)
            except Exception:
                continue
            if len(ids) != len(matrix):
                continue
            if matrix.ndim == 1:
                matrix = matrix.reshape(1, -1)
            dim = int(arr.get("dim") or matrix.shape[1])
            arrays.append({
                "embedder": str(arr.get("embedder", "st")),
                "dim": dim,
                "ids": [str(x) for x in ids],
                "matrix": np.asarray(matrix, dtype=np.float32)})
        return arrays

    def load_full(self):
        entries = self.load_meta_only()
        arrays = self.load_arrays()
        return entries, arrays

    def load(self) -> List[Dict[str, Any]]:
        entries, arrays = self.load_full()
        vectors_by_id: Dict[str, List[float]] = {}
        for arr in arrays:
            ids = arr.get("ids", [])
            matrix = arr.get("matrix")
            if matrix is None:
                continue
            for i, eid in enumerate(ids):
                try:
                    vectors_by_id[str(eid)] = matrix[i].astype(np.float32).tolist()
                except Exception:
                    continue

        for entry in entries:
            eid = str(entry.get("id", ""))
            entry["embedding"] = vectors_by_id.get(eid, [])
        return entries

    def save_all(self, entries: List[Dict[str, Any]], arrays: List[Dict[str, Any]] = None):
        if arrays is None:
            self.save_embeddings(entries)
        else:
            self.save_arrays(arrays)
        self.save_meta(entries)

    def save_meta(self, entries: List[Dict[str, Any]]):
        meta = []
        for entry in entries:
            clean = {k: v for k, v in entry.items() if k != "embedding"}
            meta.append(clean)
        self._atomic_write_json(self.meta_path, meta, indent=2)

    def save_embeddings(self, entries: List[Dict[str, Any]]):
        groups = {}
        for entry in entries:
            emb = entry.get("embedding")
            if not emb:
                continue
            eid = str(entry.get("id", "")).strip()
            if not eid:
                continue
            embedder = str(entry.get("embedder", "st"))
            dim = int(len(emb))
            key = (embedder, dim)
            if key not in groups:
                groups[key] = {"ids": [], "vecs": []}
            groups[key]["ids"].append(eid)
            groups[key]["vecs"].append(emb)
        arrays = []
        for (embedder, dim), group in groups.items():
            arrays.append({
                "embedder": embedder,
                "dim": dim,
                "ids": group["ids"],
                "matrix": np.asarray(group["vecs"], dtype=np.float32)})
        self.save_arrays(arrays)

    def save_arrays(self, arrays: List[Dict[str, Any]]):
        manifest = {
            "version": self.VERSION,
            "arrays": []}
        keep_files = set()
        for arr in arrays:
            ids = arr.get("ids") or []
            matrix = arr.get("matrix")
            if not ids or matrix is None:
                continue
            matrix = np.asarray(matrix, dtype=np.float32)
            if matrix.ndim == 1:
                matrix = matrix.reshape(1, -1)
            if len(ids) != len(matrix):
                continue
            embedder = str(arr.get("embedder", "st"))
            dim = int(arr.get("dim") or matrix.shape[1])
            safe_embedder = re.sub(r"[^a-zA-Z0-9_-]", "_", embedder)[:40]
            ids_file = f"ids_{safe_embedder}_{dim}.npy"
            matrix_file = f"matrix_{safe_embedder}_{dim}.npy"
            keep_files.add(ids_file)
            keep_files.add(matrix_file)
            ids_arr = np.array(ids, dtype=object)
            self._atomic_write_npy(os.path.join(self.db_dir, ids_file), ids_arr)
            self._atomic_write_npy(os.path.join(self.db_dir, matrix_file), matrix)
            manifest["arrays"].append({
                "embedder": embedder,
                "dim": dim,
                "count": len(ids),
                "ids_file": ids_file,
                "matrix_file": matrix_file})
        self._atomic_write_json(self.manifest_path, manifest, indent=2)
        try:
            for filename in os.listdir(self.db_dir):
                if filename.endswith(".npy") and filename not in keep_files:
                    try:
                        os.remove(os.path.join(self.db_dir, filename))
                    except Exception:
                        pass
        except Exception:
            pass
