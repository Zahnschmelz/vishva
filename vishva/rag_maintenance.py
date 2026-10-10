import os
import json
import time
import shutil
import fcntl
#import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

try:
    import psutil
except ImportError:
    psutil = None

try:
    import numpy as np
except ImportError:
    np = None

QUICK_ALIASES = {
    "user_information": "user_info",
    "userinfo": "user_info",
    "user_infos": "user_info",
    "benutzer_info": "user_info",
    "preferences": "preference",
    "prefs": "preference",
    "gewohnheiten": "preference",
    "vorlieben": "preference",
    "system_info": "system",
    "hardware": "system",
    "agent_info": "agent",
    "agent_rules": "agent",
    "wissen": "knowhow",
    "howto": "knowhow",
    "how_to": "knowhow",
    "docs": "knowhow",
    "projekt": "project",
    "task": "project",
    "misc": "miscellaneous",
    "sonstiges": "miscellaneous",
    "other": "miscellaneous",
    "auto": "miscellaneous",}


class RagMaintainer:

    STAGES = ("dedup", "prio_check", "prio_set", "category_unify", "done")

    def __init__(self, rag_manager, config: dict, daemon_ref=None, ignore_session_id=None):
        self.rag = rag_manager
        self.config = config or {}
        self.daemon = daemon_ref
        self.ignore_session_id = ignore_session_id
        self.state_path = os.path.join(
            str(config.get("rag_db_dir", "data/rag_db")),
            "..", "rag_maintenance_state.json")
        self.lock_path = os.path.join(
            str(config.get("rag_db_dir", "data/rag_db")), ".lock")
        self._lock_fd = None
        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "stage": "dedup",
            "run_started": None,
            "last_completed": None,
            "dedup_clusters": [],
            "dedup_done_clusters": 0,
            "prio_check_queue": [],
            "prio_set_queue": [],
            "prio_check_init": False,
            "prio_set_init": False,
            "category_unify_init": False,
            "category_unify_queue": [],
            "stats": {"merged": 0, "discarded": 0, "reprioritized": 0,
                      "assigned": 0, "categories_fixed": 0},
            "next_retry": None,
            "meta_backup_done": False,}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            print(f"[RAG-Maint] State save error: {e}")

    def _gates_ok(self) -> Tuple[bool, str]:
        cfg = self.config
        interval_h = float(cfg.get("rag_maintenance_interval_hours", 24))
        last = self.state.get("last_completed")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if datetime.now() - last_dt < timedelta(hours=interval_h):
                    return False, f"interval not reached ({interval_h}h)"
            except Exception:
                pass
        retry_at = self.state.get("next_retry")
        if retry_at:
            try:
                retry_dt = datetime.fromisoformat(retry_at)
                if datetime.now() < retry_dt:
                    return False, "retry pending"
            except Exception:
                pass
        idle_min = int(cfg.get("rag_maint_idle_minutes", 5))
        sessions_dir = os.path.join(os.path.dirname(self.state_path), "sessions")
        if os.path.isdir(sessions_dir):
            mtimes = []
            for f in os.listdir(sessions_dir):
                if not f.endswith(".json"):
                    continue
                if self.ignore_session_id and f[:-5] == str(self.ignore_session_id):
                    continue
                try:
                    mtimes.append(os.path.getmtime(os.path.join(sessions_dir, f)))
                except Exception:
                    pass
            if mtimes:
                newest = max(mtimes)
                if time.time() - newest < idle_min * 60:
                    return False, f"agent active (session < {idle_min}min)"
        cpu_max = int(cfg.get("rag_maint_cpu_max", 25))
        if psutil:
            cpu = psutil.cpu_percent(interval=0.5)
            if cpu > cpu_max:
                return False, f"cpu too high ({cpu:.0f}% > {cpu_max}%)"
        if psutil:
            vm = psutil.virtual_memory()
            min_pct = float(cfg.get("rag_maint_mem_min_free_percent", 50))
            min_gb = float(cfg.get("rag_maint_mem_min_free_gb", 4))
            need_pct = vm.total * (min_pct / 100.0)
            need_gb = min_gb * 1024**3
            required = max(need_pct, need_gb)
            if vm.available < required:
                return False, f"ram too low ({vm.available/1024**3:.1f}GB free)"
        if getattr(self.rag, "_st_failed", False):
            return False, "embeddings failed"
        if self.daemon and getattr(self.daemon, "_executing_task", False):
            return False, "daemon busy"
        return True, "ok"

    def _acquire_lock(self) -> bool:
        try:
            os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
            self._lock_fd = open(self.lock_path, "w")
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (IOError, OSError):
            if self._lock_fd:
                self._lock_fd.close()
                self._lock_fd = None
            return False

    def _release_lock(self):
        if self._lock_fd:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._lock_fd.close()
            except Exception:
                pass
            self._lock_fd = None

    def _ensure_meta_backup(self):
        if self.state.get("meta_backup_done"):
            return
        meta_path = os.path.join(
            str(self.config.get("rag_db_dir", "data/rag_db")), "meta.json")
        if os.path.exists(meta_path):
            try:
                shutil.copy2(meta_path, meta_path + ".bak")
                self.state["meta_backup_done"] = True
            except Exception:
                pass

    def tick(self):
        if not bool(self.config.get("rag_maintenance_enabled", True)):
            return
        ok, reason = self._gates_ok()
        if not ok:
            if reason == "retry pending":
                pass
            return
        if not self._acquire_lock():
            return
        try:
            self._ensure_meta_backup()
            if self.state.get("run_started") is None:
                self.state["run_started"] = datetime.now().isoformat()
                self.state["meta_backup_done"] = False
                self._ensure_meta_backup()
                print(f"[RAG-Maint] Run started at {self.state['run_started']}")
            stage = self.state.get("stage", "dedup")
            if stage == "dedup":
                self._tick_dedup()
            elif stage == "prio_check":
                self._tick_prio_check()
            elif stage == "prio_set":
                self._tick_prio_set()
            elif stage == "category_unify":
                self._tick_category_unify()
            elif stage == "done":
                self._finish_run()
            self._save_state()
        finally:
            self._release_lock()

    def _finish_run(self):
        self.state["last_completed"] = datetime.now().isoformat()
        self.state["run_started"] = None
        self.state["stage"] = "dedup"
        self.state["dedup_clusters"] = []
        self.state["dedup_done_clusters"] = 0
        self.state["prio_check_queue"] = []
        self.state["prio_set_queue"] = []
        self.state["category_unify_queue"] = []
        self.state["meta_backup_done"] = False
        self.state["prio_check_init"] = False
        self.state["prio_set_init"] = False
        self.state["category_unify_init"] = False
        stats = self.state.get("stats", {})
        print(f"[RAG-Maint] Run completed. Stats: {stats}")
        if bool(self.config.get("scheduler_notifications_enabled", True)):
            try:
                import subprocess
                msg = (f"RAG-Wartung abgeschlossen: "
                       f"{stats.get('merged',0)} Merges, "
                       f"{stats.get('reprioritized',0)} Reprios, "
                       f"{stats.get('assigned',0)} Assigned, "
                       f"{stats.get('categories_fixed',0)} Kategorien")
                subprocess.Popen(
                    ["notify-send", "Vishva", msg, "-u", "normal"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

    def _excluded(self, entry) -> bool:
        essential = set(self.config.get("rag_essential_categories", ["essential"]))
        excluded = set(self.config.get("rag_exclude_maintenance_categories", ["essential", "knowledge"]))
        cat = str(entry.get("category", ""))
        return cat in essential or cat in excluded

    def _tick_dedup(self):
        if np is None or not getattr(self.rag, "_matrix_mode", False):
            self.state["stage"] = "prio_check"
            return
        threshold = float(self.config.get("rag_maint_dedup_threshold", 0.80))
        max_merges = int(self.config.get("rag_maint_max_merges_per_tick", 3))
        dry_run = bool(self.config.get("rag_maint_dry_run", False))
        if not self.state.get("dedup_clusters"):
            clusters = self._find_dedup_clusters(threshold)
            self.state["dedup_clusters"] = clusters
            self.state["dedup_done_clusters"] = 0
            print(f"[RAG-Maint] Dedup: {len(clusters)} Cluster gefunden")
        clusters = self.state.get("dedup_clusters", [])
        done = self.state.get("dedup_done_clusters", 0)
        merges_this_tick = 0
        while done < len(clusters) and merges_this_tick < max_merges:
            cluster = clusters[done]
            done += 1
            self.state["dedup_done_clusters"] = done
            if len(cluster) < 2:
                continue
            sources = set()
            for eid in cluster:
                entry = self.rag._entry_by_id.get(str(eid))
                if entry:
                    sources.add(str(entry.get("source", "")))
            if len(sources) == 1:
                continue
            skip = False
            for eid in cluster:
                entry = self.rag._entry_by_id.get(str(eid))
                if entry and self._excluded(entry):
                    skip = True
                    break
            if skip:
                continue
            if dry_run:
                texts = []
                for eid in cluster:
                    e = self.rag._entry_by_id.get(str(eid))
                    if e:
                        texts.append(e.get("text", "")[:60])
                print(f"[RAG-Maint][DRY] Dedup-Cluster: {texts}")
                merges_this_tick += 1
                continue
            if bool(self.config.get("rag_maint_dedup_meta_confirm", True)):
                decision = self._meta_dedup_confirm(cluster)
                if not decision or decision.get("action") == "KEEP_ALL":
                    continue
            self._merge_cluster(cluster)
            merges_this_tick += 1
            self.state["stats"]["merged"] += 1
        if done >= len(clusters):
            self.state["stage"] = "prio_check"
            self.state["dedup_clusters"] = []
            self.state["dedup_done_clusters"] = 0
            print(f"[RAG-Maint] Dedup abgeschlossen. {self.state['stats']['merged']} Merges.")

    def _meta_category_batch(self, batch_ids: List[str]):
        try:
            from .background_agent import MetaModelClient
        except ImportError:
            return
        meta = MetaModelClient(self.config)
        if not meta.available:
            return
        canonical = self.config.get("rag_categories", [])
        entries_text = []
        for eid in batch_ids:
            entry = self.rag._entry_by_id.get(str(eid))
            if not entry:
                continue
            entries_text.append(
                f"ID: {eid} | current_category: {entry.get('category', '')} | "
                f"Text: {str(entry.get('text', ''))[:150]}")
        if not entries_text:
            return
        prompt = (
            "Assign each entry to the best matching category.\n"
            f"Allowed categories ONLY: {', '.join(canonical)}\n"
            "Meaning:\n"
            "- user_info: identity, name, job, relationships, facts about the user\n"
            "- preference: likes/dislikes, habits, communication style\n"
            "- system: hardware, OS, paths, services, devices\n"
            "- agent: assistant identity, rules, instructions\n"
            "- knowhow: general knowledge, how-tos, references\n"
            "- project: current project facts\n"
            "- miscellaneous: greetings, smalltalk, unclear content\n\n"
            + "\n".join(entries_text) + "\n\n"
            'Answer as JSON array: [{"id": "...", "category": "..."}]')
        response = meta.chat(prompt, max_tokens=400, temperature=0.0)
        if not response:
            return
        try:
            start = response.find("[")
            end = response.rfind("]") + 1
            if start < 0 or end <= start:
                return
            results = json.loads(response[start:end])
        except Exception:
            return
        canonical_set = {str(c).lower() for c in canonical}
        changed = 0
        for item in results:
            eid = str(item.get("id", ""))
            new_cat = str(item.get("category", "")).strip().lower()
            if not eid or new_cat not in canonical_set:
                continue
            entry = self.rag._entry_by_id.get(eid)
            if entry and str(entry.get("category", "")).lower() != new_cat:
                entry["category"] = new_cat
                changed += 1
                self.state["stats"]["categories_fixed"] += 1
        if changed:
            try:
                self.rag._save_meta()
            except Exception:
                pass
            print(f"[RAG-Maint] Category-Unify: {changed} Kategorien korrigiert")

    def _find_dedup_clusters(self, threshold: float) -> List[List[str]]:
        if np is None or not getattr(self.rag, "_matrix_mode", False):
            return []
        clusters = []
        visited = set()
        for (embedder, dim), arr in self.rag._rag_arrays.items():
            matrix = arr.get("matrix")
            ids = [str(x) for x in arr.get("ids", [])]
            if matrix is None or not ids:
                continue
            if matrix.ndim == 1:
                matrix = matrix.reshape(1, -1)
            n = len(ids)
            for i in range(n):
                eid_i = ids[i]
                if eid_i in visited:
                    continue
                entry_i = self.rag._entry_by_id.get(eid_i)
                if not entry_i or self._excluded(entry_i):
                    visited.add(eid_i)
                    continue
                try:
                    sim_row = matrix[i] @ matrix.T
                except Exception:
                    continue
                cluster = [eid_i]
                visited.add(eid_i)
                for j in range(i + 1, n):
                    eid_j = ids[j]
                    if eid_j in visited:
                        continue
                    entry_j = self.rag._entry_by_id.get(eid_j)
                    if not entry_j or self._excluded(entry_j):
                        continue
                    if entry_i.get("source") == entry_j.get("source"):
                        continue
                    if float(sim_row[j]) >= threshold:
                        cluster.append(eid_j)
                        visited.add(eid_j)
                if len(cluster) >= 2:
                    clusters.append(cluster)
        return clusters

    def _meta_dedup_confirm(self, cluster: List[str]) -> Optional[Dict]:
        try:
            from .background_agent import MetaModelClient
        except ImportError:
            return None
        meta = MetaModelClient(self.config)
        if not meta.available:
            return None
        texts = []
        for eid in cluster:
            entry = self.rag._entry_by_id.get(str(eid))
            if entry:
                texts.append(entry.get("text", "")[:200])
        max_merge_chars = int(self.config.get("rag_maint_max_merge_chars", 1000))
        prompt = (
            f"These entries are near-duplicates. Decide:\n"
            f"1. MERGE — if they are variants of the same fact or closely related facts "
            f"of the SAME type. Combine them into ONE consolidated entry. "
            f"The merged text must stay under {max_merge_chars} characters.\n"
            f"2. KEEP_BEST — if they overlap but are not cleanly mergeable: "
            f"keep the most complete entry, discard the rest.\n"
            f"3. KEEP_ALL — if they are actually distinct facts.\n\n"
            f"Entries:\n" + "\n".join(f"- {t}" for t in texts) + "\n\n"
            f"Answer as JSON: {{\"action\": \"MERGE|KEEP_BEST|KEEP_ALL\", \"text\": \"...\"}}")
        response = meta.chat(prompt, max_tokens=200, temperature=0.0)
        if not response:
            return None
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(response[start:end])
        except Exception:
            pass
        return None

    def _merge_cluster(self, cluster: List[str]):
        entries = []
        for eid in cluster:
            entry = self.rag._entry_by_id.get(str(eid))
            if entry and not self._excluded(entry):
                entries.append(entry)
        if len(entries) < 2:
            return
        entries.sort(key=lambda e: (
            -int(e.get("priority", 1)),
            -int(e.get("access_count", 0)),
            str(e.get("created_at", ""))))
        best = entries[0]
        others = entries[1:]
        max_merge_chars = int(self.config.get("rag_maint_max_merge_chars", 1000))
        merged_text = best.get("text", "")
        for other in others:
            candidate = merged_text + "\n" + other.get("text", "")
            if len(candidate) <= max_merge_chars:
                merged_text = candidate
        best["text"] = merged_text
        best["metadata"] = best.get("metadata", {})
        best["metadata"]["merged_from"] = [str(e.get("id", "")) for e in others]
        best["metadata"]["merged_at"] = datetime.now().isoformat()
        remove_ids = {str(e.get("id", "")) for e in others}
        self.rag.entries = [e for e in self.rag.entries if str(e.get("id", "")) not in remove_ids]
        self.rag._rebuild_entry_index()
        self.state["stats"]["discarded"] += len(others)
        try:
            self.rag._save()
        except Exception:
            pass

    def _tick_prio_check(self):
        batch_size = int(self.config.get("rag_maint_batch_size", 20))
        dry_run = bool(self.config.get("rag_maint_dry_run", False))
        if not self.state.get("prio_check_init"):
            queue = [
                str(e.get("id", ""))
                for e in self.rag.entries
                if not self._excluded(e)
                and e.get("priority") is not None
                and e.get("id")]
            self.state["prio_check_queue"] = queue
            self.state["prio_check_init"] = True
            print(f"[RAG-Maint] Prio-Check: {len(queue)} Einträge in Queue")
        queue = self.state.get("prio_check_queue", [])
        if not queue:
            print("[RAG-Maint] Prio-Check abgeschlossen.")
            self.state["stage"] = "prio_set"
            self.state["prio_check_init"] = False
            return
        batch = queue[:batch_size]
        self.state["prio_check_queue"] = queue[batch_size:]
        if dry_run:
            print(f"[RAG-Maint][DRY] Prio-Check Batch: {len(batch)} Einträge")
            return
        self._meta_prio_batch(batch, mode="check")

    def _tick_prio_set(self):
        batch_size = int(self.config.get("rag_maint_batch_size", 20))
        dry_run = bool(self.config.get("rag_maint_dry_run", False))
        if not self.state.get("prio_set_init"):
            queue = [
                str(e.get("id", ""))
                for e in self.rag.entries
                if not self._excluded(e)
                and e.get("priority") is None
                and e.get("id")]
            self.state["prio_set_queue"] = queue
            self.state["prio_set_init"] = True
            print(f"[RAG-Maint] Prio-Set: {len(queue)} Einträge in Queue")
        queue = self.state.get("prio_set_queue", [])
        if not queue:
            print("[RAG-Maint] Prio-Set abgeschlossen.")
            self.state["stage"] = "category_unify"
            self.state["prio_set_init"] = False
            return
        batch = queue[:batch_size]
        self.state["prio_set_queue"] = queue[batch_size:]
        if dry_run:
            print(f"[RAG-Maint][DRY] Prio-Set Batch: {len(batch)} Einträge")
            return
        self._meta_prio_batch(batch, mode="set")

    def _meta_prio_batch(self, batch_ids: List[str], mode: str) -> int:
        try:
            from .background_agent import MetaModelClient
        except ImportError:
            return 0
        meta = MetaModelClient(self.config)
        if not meta.available:
            return 0
        entries_text = []
        for eid in batch_ids:
            entry = self.rag._entry_by_id.get(str(eid))
            if not entry:
                continue
            prio = entry.get("priority", "?")
            access = entry.get("access_count", 0)
            age = entry.get("age_cycles", 0)
            cat = entry.get("category", "?")
            text = str(entry.get("text", ""))[:150]
            entries_text.append(
                f"ID: {eid} | Prio: {prio} | Access: {access} | Age: {age} | "
                f"Cat: {cat} | Text: {text}")
        if not entries_text:
            return 0
        if mode == "check":
            prompt = (
                "Review these RAG entries and adjust their priority if needed.\n"
                "Priority range: 0=irrelevant, 1=minor, 2=useful, 3=critical.\n"
                "Consider: Is the info still relevant? Was it ever accessed?\n"
                "Set priority to 0 for irrelevant entries (they will age out naturally).\n"
                "Only change a priority if clearly justified.\n\n"
                + "\n".join(entries_text) + "\n\n"
                "Answer ONLY with a JSON array: "
                "[{\"id\": \"...\", \"priority\": N}]")
        else:
            prompt = (
                "These RAG entries have no priority assigned. "
                "Assign a priority to each.\n"
                "Priority range: 0=irrelevant, 1=minor, 2=useful, 3=critical.\n"
                "Priority guidance by category:\n"
                "- user_info, preference, system, agent: priority 2-3\n"
                "- knowhow: priority 1-2\n"
                "- project: priority 0-1\n"
                "- miscellaneous: priority 0-3 based on relevance\n\n"
                + "\n".join(entries_text) + "\n\n"
                "Answer ONLY with a JSON array: "
                "[{\"id\": \"...\", \"priority\": N}]")
        response = meta.chat(prompt, max_tokens=600, temperature=0.1)
        if not response:
            return 0
        try:
            start = response.find("[")
            end = response.rfind("]") + 1
            if start < 0 or end <= start:
                return 0
            results = json.loads(response[start:end])
        except Exception:
            return 0
        batch_set = {str(x) for x in batch_ids}
        changed = 0
        for item in results:
            if not isinstance(item, dict):
                continue
            eid = str(item.get("id", ""))
            if not eid or eid not in batch_set:
                continue
            new_prio = item.get("priority")
            if new_prio is None:
                continue
            try:
                new_prio = max(0, min(3, int(new_prio)))
            except Exception:
                continue
            entry = self.rag._entry_by_id.get(eid)
            if not entry:
                continue
            old_prio = entry.get("priority")
            if old_prio != new_prio:
                entry["priority"] = new_prio
                entry["metadata"] = entry.get("metadata", {})
                entry["metadata"]["priority_set_by"] = "maintenance"
                entry["metadata"]["priority_set_at"] = datetime.now().isoformat()
                changed += 1
                if mode == "check":
                    self.state["stats"]["reprioritized"] += 1
                else:
                    self.state["stats"]["assigned"] += 1
        if changed:
            try:
                self.rag._save_meta()
            except Exception:
                pass
            print(f"[RAG-Maint] Prio-{mode}: {changed} Einträge aktualisiert")
        return changed

    def _tick_category_unify(self):
        batch_size = int(self.config.get("rag_maint_batch_size", 20))
        dry_run = bool(self.config.get("rag_maint_dry_run", False))
        if not self.state.get("category_unify_init"):
            canonical = {str(c).lower() for c in self.config.get("rag_categories", [])}
            reserved = {str(c).lower() for c in self.config.get("rag_reserved_categories", ["essential", "knowledge"])}
            valid = canonical | reserved
            recheck = {str(self.config.get("rag_category_fallback", "miscellaneous")).lower()}
            queue = [
                str(e.get("id", ""))
                for e in self.rag.entries
                if e.get("id")
                and not self._excluded(e)
                and (
                    str(e.get("category", "")).lower() not in valid
                    or str(e.get("category", "")).lower() in recheck)]
            self.state["category_unify_queue"] = queue
            self.state["category_unify_init"] = True
            print(f"[RAG-Maint] Category-Unify: {len(queue)} Einträge in Meta-Queue")
        queue = self.state.get("category_unify_queue", [])
        if not queue:
            self.state["stage"] = "done"
            return
        batch = queue[:batch_size]
        self.state["category_unify_queue"] = queue[batch_size:]
        if dry_run:
            print(f"[RAG-Maint][DRY] Category-Unify Batch: {len(batch)} Einträge")
            return
        self._meta_category_batch(batch)
