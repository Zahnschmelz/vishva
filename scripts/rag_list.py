#!/usr/bin/env python3
"""
RAG List Tool - Zeigt Einträge aus der RAG-Datenbank an und ermöglicht manuelles Aufräumen.

Unterstützt das neue Format:
  rag_db/meta.json
  rag_db/manifest.json
  rag_db/*.npy

Beispiele:
  python rag_list.py
  python rag_list.py --full
  python rag_list.py --category auto
  python rag_list.py --source conversation
  python rag_list.py --sort-by cycles
  python rag_list.py --min-access 5
  python rag_list.py --delete 3
  python rag_list.py --delete-unused
  python rag_list.py --delete-category auto
  python rag_list.py --stats
"""

import os
import sys
import json
import argparse
import shutil
from typing import List, Dict, Any

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"


def load_config(path: str = "config.json") -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️  Config-Ladefehler: {e}")
        return {}


def get_manager(config_path: str = "config.json"):
    """
    Lädt den RAGManager, aber ohne Startup-Indexing.
    Dadurch kann rag_list.py gefahrlos lesen/löschen.
    """
    cfg = load_config(config_path)

    # Wichtig: rag_list.py soll nicht beim Start neu indexieren
    cfg["rag_index_startup"] = False

    try:
        from rag_manager import RAGManager
    except Exception as e:
        print(f"❌ rag_manager.py konnte nicht importiert werden: {e}")
        sys.exit(1)

    try:
        manager = RAGManager(cfg)
        return manager
    except Exception as e:
        print(f"❌ RAGManager konnte nicht initialisiert werden: {e}")
        sys.exit(1)


def backup_manager_db(manager):
    """
    Erstellt ein Backup des neuen rag_db/-Ordners oder der alten rag_db.json.
    """
    try:
        db_dir = getattr(manager, "db_dir", None)

        if db_dir and os.path.isdir(db_dir):
            backup = db_dir.rstrip("/") + ".bak"

            if os.path.exists(backup):
                shutil.rmtree(backup)

            shutil.copytree(db_dir, backup)
            print(f"💾 Backup: {backup}")
            return

        if os.path.exists(manager.db_path):
            backup = manager.db_path + ".bak"
            shutil.copy2(manager.db_path, backup)
            print(f"💾 Backup: {backup}")

    except Exception as e:
        print(f"⚠️  Backup fehlgeschlagen: {e}")


def save_manager_db(manager, entries: List[Dict[str, Any]]):
    """
    Speichert die geänderte Eintragsliste über den RAGManager.
    Dadurch werden meta.json + .npy-Dateien aktualisiert.
    """
    manager.entries = entries

    try:
        manager._save()
        print("💾 Gespeichert (rag_db/).")
    except Exception as e:
        print(f"❌ Speichern fehlgeschlagen: {e}")
        sys.exit(1)


def truncate(text: str, max_len: int = 80) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) > max_len:
        return text[:max_len] + "…"
    return text


def print_entry(idx: int, entry: Dict[str, Any], full: bool = False):
    text = entry.get("text", "")
    source = entry.get("source", "?")
    category = entry.get("category", "?")
    entry_id = str(entry.get("id", "?"))[:12]
    access_count = entry.get("access_count", 0)
    age_cycles = entry.get("age_cycles", 0)
    created_at = entry.get("created_at", "?")
    last_access = entry.get("last_access", "?")

    if access_count == 0:
        access_icon = "🔴"
    elif access_count <= 2:
        access_icon = "🟡"
    else:
        access_icon = "🟢"

    print(f"\n{'─' * 70}")
    print(f"  #{idx:3}  {access_icon} Zugriffe: {access_count}  |  Zyklen: {age_cycles}")
    print(f"  🆔 ID:       {entry_id}")
    print(f"  📁 Quelle:   {source}")
    print(f"  🏷️  Kategorie: {category}")
    print(f"  🕐 Erstellt: {created_at}")
    print(f"  🕐 Zugriff:  {last_access}")

    if full:
        print("  📄 Text:")
        for line in text.split("\n"):
            print(f"     {line}")
    else:
        print(f"  📄 Text:     {truncate(text)}")


def print_stats(entries: List[Dict[str, Any]]):
    if not entries:
        print("Keine Einträge.")
        return

    categories = {}
    sources = {}
    access_dist = {"0": 0, "1-2": 0, "3-5": 0, "6+": 0}
    never_accessed = 0
    total_access = 0

    for e in entries:
        cat = e.get("category", "unknown")
        src = e.get("source", "unknown")
        ac = e.get("access_count", 0)

        categories[cat] = categories.get(cat, 0) + 1

        src_short = src.split(":")[0] if ":" in src else src
        sources[src_short] = sources.get(src_short, 0) + 1

        total_access += ac

        if ac == 0:
            never_accessed += 1
            access_dist["0"] += 1
        elif ac <= 2:
            access_dist["1-2"] += 1
        elif ac <= 5:
            access_dist["3-5"] += 1
        else:
            access_dist["6+"] += 1

    print(f"\n{'=' * 70}")
    print("  📊 RAG Datenbank Statistiken")
    print(f"{'=' * 70}")
    print(f"  Einträge gesamt:      {len(entries)}")
    print(f"  Gesamt-Zugriffe:      {total_access}")
    print(f"  Nie abgerufen:        {never_accessed} ({never_accessed / len(entries) * 100:.0f}%)")

    print("\n📁 Kategorien:")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        print(f"     {cat}: {count}")

    print("\n📂 Quellen (Prefix):")
    for src, count in sorted(sources.items(), key=lambda x: -x[1]):
        print(f"     {src}: {count}")

    print("\n📈 Zugriffsverteilung:")
    print(f"     0 Zugriffe:   {access_dist['0']}")
    print(f"     1-2:          {access_dist['1-2']}")
    print(f"     3-5:          {access_dist['3-5']}")
    print(f"     6+:           {access_dist['6+']}")
    print(f"{'=' * 70}")


def filter_entries(entries: List[Dict[str, Any]], args) -> List[Dict[str, Any]]:
    result = entries

    if args.category:
        result = [e for e in result if e.get("category", "") == args.category]

    if args.source:
        result = [e for e in result if str(e.get("source", "")).startswith(args.source)]

    if args.min_access is not None:
        result = [e for e in result if e.get("access_count", 0) >= args.min_access]

    if args.max_cycles is not None:
        result = [e for e in result if e.get("age_cycles", 0) >= args.max_cycles]

    if args.never_accessed:
        result = [e for e in result if e.get("access_count", 0) == 0]

    return result


def sort_entries(entries: List[Dict[str, Any]], sort_by: str) -> List[Dict[str, Any]]:
    if sort_by == "access":
        return sorted(entries, key=lambda e: e.get("access_count", 0))
    elif sort_by == "cycles":
        return sorted(entries, key=lambda e: -e.get("age_cycles", 0))
    elif sort_by == "date":
        return sorted(entries, key=lambda e: e.get("created_at", ""))
    elif sort_by == "text":
        return sorted(entries, key=lambda e: e.get("text", "")[:50])
    return entries


def delete_entries(entries: List[Dict[str, Any]], to_delete: set) -> List[Dict[str, Any]]:
    return [e for i, e in enumerate(entries) if i not in to_delete]


def main():
    parser = argparse.ArgumentParser(
        description="RAG List Tool - Einträge anzeigen und aufräumen",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Lösch-Beispiele:
  python rag_list.py --delete 3
  python rag_list.py --delete 1 5 8
  python rag_list.py --delete-unused
  python rag_list.py --delete-unused --yes
  python rag_list.py --delete-category auto
  python rag_list.py --delete-source conversation
"""
    )

    parser.add_argument("--full", action="store_true", help="Zeige vollen Text")
    parser.add_argument("--category", help="Filter: Kategorie")
    parser.add_argument("--source", help="Filter: Quellen-Prefix")
    parser.add_argument("--min-access", type=int, help="Filter: min. Zugriffe")
    parser.add_argument("--max-cycles", type=int, help="Filter: min. Alter-Zyklen")
    parser.add_argument("--never-accessed", action="store_true", help="Filter: nur nie abgerufene")
    parser.add_argument("--sort-by", choices=["access", "cycles", "date", "text"], default=None, help="Sortierung")
    parser.add_argument("--stats", action="store_true", help="Zeige nur Statistiken")
    parser.add_argument("--json", action="store_true", help="JSON-Ausgabe")
    parser.add_argument("--include-embeddings", action="store_true", help="JSON: Embeddings mit ausgeben")
    parser.add_argument("--config", default="config.json", help="Pfad zur config.json")

    # Lösch-Optionen
    parser.add_argument("--delete", nargs="+", type=int, help="Lösche Einträge nach Index (1-basiert)")
    parser.add_argument("--delete-unused", action="store_true", help="Lösche alle Einträge mit 0 Zugriffen")
    parser.add_argument("--delete-category", help="Lösche alle Einträge einer Kategorie")
    parser.add_argument("--delete-source", help="Lösche alle Einträge einer Quelle (Prefix)")
    parser.add_argument("--yes", "-y", action="store_true", help="Überspringe Bestätigung")

    args = parser.parse_args()

    manager = get_manager(args.config)
    entries = manager.entries

    if not entries:
        print("RAG-Datenbank ist leer.")
        sys.exit(0)

    # === STATS MODUS ===
    if args.stats:
        print_stats(entries)
        sys.exit(0)

    # === LÖSCH-MODUS ===
    if args.delete or args.delete_unused or args.delete_category or args.delete_source:
        to_delete = set()

        if args.delete:
            for idx in args.delete:
                if 1 <= idx <= len(entries):
                    to_delete.add(idx - 1)
                else:
                    print(f"⚠️  Index {idx} außerhalb Bereichs (1-{len(entries)})")

        if args.delete_unused:
            for i, e in enumerate(entries):
                if e.get("access_count", 0) == 0:
                    to_delete.add(i)

        if args.delete_category:
            for i, e in enumerate(entries):
                if e.get("category", "") == args.delete_category:
                    to_delete.add(i)

        if args.delete_source:
            for i, e in enumerate(entries):
                if str(e.get("source", "")).startswith(args.delete_source):
                    to_delete.add(i)

        if not to_delete:
            print("Nichts zu löschen.")
            sys.exit(0)

        print(f"\n⚠️  {len(to_delete)} Einträge werden gelöscht:")

        for idx in sorted(to_delete):
            e = entries[idx]
            print(f"  #{idx + 1}: {truncate(e.get('text', ''), 60)}")

        if not args.yes:
            confirm = input(f"\nWirklich löschen? (j/n): ").strip().lower()
            if confirm not in ("j", "y", "ja", "yes"):
                print("Abgebrochen.")
                sys.exit(0)

        backup_manager_db(manager)

        new_entries = delete_entries(entries, to_delete)
        save_manager_db(manager, new_entries)

        print(f"✅ {len(to_delete)} Einträge gelöscht. {len(new_entries)} verbleiben.")
        print("⚠️  Falls der Agent läuft, danach neu starten.")
        sys.exit(0)

    # === ANZEIGE-MODUS ===
    filtered = filter_entries(entries, args)

    if args.sort_by:
        filtered = sort_entries(filtered, args.sort_by)

    if args.json:
        output = []

        for e in filtered:
            clean = {k: v for k, v in e.items() if k != "embedding"}

            if args.include_embeddings:
                clean["embedding"] = e.get("embedding", [])

            output.append(clean)

        print(json.dumps(output, indent=2, ensure_ascii=False))
        sys.exit(0)

    print(f"\n📦 {len(filtered)} von {len(entries)} Einträgen:")

    # Original-Indizes behalten, damit --delete weiterhin funktioniert
    filtered_ids = {id(e) for e in filtered}
    original_indices = [i for i, e in enumerate(entries) if id(e) in filtered_ids]

    for orig_idx in original_indices:
        print_entry(orig_idx + 1, entries[orig_idx], full=args.full)

    print(f"\n{'─' * 70}")
    print("  Zum Löschen: python rag_list.py --delete <nummern>")
    print("  Beispiel: python rag_list.py --delete 1 3 5")
    print(f"{'─' * 70}")


if __name__ == "__main__":
    main()
