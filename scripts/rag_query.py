#!/usr/bin/env python3
"""
RAG Query Tool - Abfragen auf dem neuen rag_db/-Format.

Nutzt:
  rag_db/meta.json
  rag_db/manifest.json
  rag_db/*.npy

Beispiele:
  python rag_query.py "Wie heiße ich?"
  python rag_query.py "Alex" --top-k 10
  python rag_query.py "Projekt" --min-score 0.5 --verbose
  python rag_query.py "Name" --json
  python rag_query.py --stats
"""

import os
import sys
import json
import math
import argparse
from typing import List, Dict, Any, Tuple

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"


def load_config(path: str = "config.json") -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)

        return {
            str(k).strip(): (v.strip() if isinstance(v, str) else v)
            for k, v in config.items()
        }
    except Exception as e:
        print(f"⚠️  Config-Ladefehler: {e}")
        return {}


def get_manager(config_path: str = "config.json"):
    """
    Lädt den RAGManager ohne Startup-Indexing.
    """
    cfg = load_config(config_path)

    # rag_query.py soll nicht automatisch indexieren
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


def format_score_bar(score: float, width: int = 20) -> str:
    filled = int(score * width)
    bar = "█" * filled + "░" * (width - filled)

    if score >= 0.7:
        color = "🟢"
    elif score >= 0.4:
        color = "🟡"
    else:
        color = "🔴"

    return f"{color} [{bar}] {score:.4f}"


def format_entry(entry: Dict[str, Any], score: float, index: int, verbose: bool = False) -> str:
    text = entry.get("text", "")
    source = entry.get("source", "?")
    category = entry.get("category", "?")
    entry_id = str(entry.get("id", "?"))[:12]
    access_count = entry.get("access_count", 0)
    age_cycles = entry.get("age_cycles", 0)
    last_access = entry.get("last_access", "?")
    created_at = entry.get("created_at", "?")

    display_text = text[:200] + "..." if len(text) > 200 else text
    display_text = display_text.replace("\n", " ")

    lines = []
    lines.append(f"\n{'=' * 60}")
    lines.append(f"  #{index + 1}  |  Score: {format_score_bar(score)}")
    lines.append(f"  📄 Text:     {display_text}")
    lines.append(f"  📁 Quelle:   {source}")
    lines.append(f"  🏷️  Kategorie: {category}")
    lines.append(f"  🆔 ID:       {entry_id}")

    if verbose:
        lines.append(f"  📊 Zugriffe: {access_count}  |  Alter-Zyklen: {age_cycles}")
        lines.append(f"  🕐 Erstellt: {created_at}")
        lines.append(f"  🕐 Letzter Zugriff: {last_access}")

        if len(text) > 200:
            lines.append(f"\n📝 Voller Text:\n{text}")

    return "\n".join(lines)


def print_stats(manager):
    entries = manager.entries

    if not entries:
        print("RAG-Datenbank ist leer.")
        return

    categories = {}
    sources = {}
    access_counts = []
    age_cycles = []
    has_embedding = 0
    embedding_dims = set()

    for e in entries:
        cat = e.get("category", "unknown")
        src = e.get("source", "unknown")

        categories[cat] = categories.get(cat, 0) + 1

        src_short = src.split("/")[0] if "/" in src else src
        sources[src_short] = sources.get(src_short, 0) + 1

        access_counts.append(e.get("access_count", 0))
        age_cycles.append(e.get("age_cycles", 0))

        emb = e.get("embedding", [])
        if emb:
            has_embedding += 1
            embedding_dims.add(len(emb))

    total_access = sum(access_counts)
    never_accessed = sum(1 for c in access_counts if c == 0)
    avg_age = sum(age_cycles) / len(age_cycles) if age_cycles else 0

    print(f"\n{'=' * 60}")
    print("  📊 RAG Datenbank Statistiken")
    print(f"{'=' * 60}")
    print(f"  Einträge gesamt:      {len(entries)}")
    print(f"  Mit Embedding:        {has_embedding} ({has_embedding / len(entries) * 100:.0f}%)")
    print(f"  Embedding-Dims:       {embedding_dims if embedding_dims else 'keine'}")
    print(f"  Gesamt-Zugriffe:      {total_access}")
    print(f"  Nie abgerufen:        {never_accessed} ({never_accessed / len(entries) * 100:.0f}%)")
    print(f"  Ø Alter-Zyklen:       {avg_age:.1f}")

    print("\n📁 Kategorien:")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        print(f"     {cat}: {count}")

    print("\n📂 Quellen (gekürzt):")
    for src, count in sorted(sources.items(), key=lambda x: -x[1]):
        print(f"     {src}: {count}")


def score_entries(
    manager,
    query: str,
    min_score: float,
    no_embed: bool
) -> Tuple[List[Tuple[float, Dict[str, Any]]], str, int]:
    """
    Scoret Einträge über den RAGManager.

    Wichtig:
    - nutzt manager._embed()
    - nutzt manager._score_all()
    - verändert keine Access-Tracking-Daten
    """
    qv = manager._embed(query)

    if not qv:
        return [], ("hash" if manager._st_failed else "st"), 0

    current_embedder = "hash" if manager._st_failed else "st"

    # Normale Suche über vorhandene Embeddings
    scored = manager._score_all(qv, current_embedder)

    # Optional: Einträge ohne gespeichertes Embedding temporär embedden
    if not no_embed:
        scored_ids = {id(entry) for _, entry in scored}

        for entry in manager.entries:
            if id(entry) in scored_ids:
                continue

            emb = entry.get("embedding") or []

            # Wenn ein Embedding vorhanden ist, aber nicht gescort wurde,
            # passt wahrscheinlich Embedder/Dimension nicht. Dann nicht erzwingen.
            if emb:
                continue

            emb = manager._embed(entry.get("text", ""))
            if not emb:
                continue

            if len(emb) != len(qv):
                continue

            score = manager._cosine(qv, emb)
            scored.append((score, entry))

    scored = [
        (float(score), entry)
        for score, entry in scored
        if score >= min_score
    ]

    scored.sort(key=lambda x: x[0], reverse=True)

    return scored, current_embedder, len(qv)


def main():
    parser = argparse.ArgumentParser(
        description="RAG Query Tool - Abfragen auf rag_db/ mit Score-Anzeige",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  python rag_query.py "Wie heiße ich?"
  python rag_query.py "Alex" --top-k 10
  python rag_query.py "Projekt" --min-score 0.5 --verbose
  python rag_query.py "Name" --json
  python rag_query.py --stats
"""
    )

    parser.add_argument("query", nargs="?", help="Suchanfrage")
    parser.add_argument("--top-k", type=int, help="Anzahl Ergebnisse (überschreibt config)")
    parser.add_argument("--min-score", type=float, default=0.0, help="Mindest-Score (0.0-1.0)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Zeige alle Details und vollen Text")
    parser.add_argument("--json", action="store_true", help="JSON-Ausgabe statt formatierter Ausgabe")
    parser.add_argument("--stats", action="store_true", help="Zeige DB-Statistiken statt Suche")
    parser.add_argument("--config", default="config.json", help="Pfad zur config.json")
    parser.add_argument("--no-embed", action="store_true", help="Embedde fehlende Einträge nicht temporär")
    parser.add_argument("--db", default=None, help="DEPRECATED: wird ignoriert. Genutzt wird rag_db/ aus der config.")

    args = parser.parse_args()

    if args.db:
        print("⚠️  --db wird ignoriert. rag_query.py nutzt jetzt rag_db/ über den RAGManager.\n")

    manager = get_manager(args.config)
    entries = manager.entries

    if not entries:
        print("❌ RAG-Datenbank ist leer.")
        sys.exit(0)

    # Stats-Modus
    if args.stats:
        print_stats(manager)
        sys.exit(0)

    # Query-Modus
    if not args.query:
        parser.print_help()
        sys.exit(1)

    cfg = manager.config

    rag_top_k = args.top_k or manager.top_k
    rag_max_context = cfg.get("rag_max_context_chars", 2500)
    rag_enabled = bool(cfg.get("rag_enabled", True))

    print(f"⚙️  Config: top_k={rag_top_k}, max_context={rag_max_context}, enabled={rag_enabled}")
    print(f"📊 Embedding-Modell: {cfg.get('rag_embedding_model', 'default')}")
    print(f"📦 Einträge in DB: {len(entries)}")

    if not rag_enabled:
        print("⚠️  RAG ist in config.json deaktiviert (rag_enabled=false).")
        print("    Abfrage wird trotzdem ausgeführt.\n")

    scored_results, current_embedder, vector_dim = score_entries(
        manager,
        args.query,
        args.min_score,
        args.no_embed
    )

    mode = "hash" if current_embedder == "hash" else "semantic"

    print(f"🔍 Embedding-Modus: {mode}\n")
    print(f"🔎 Query: \"{args.query}\"")
    print(f"   Vektor-Dimensionen: {vector_dim}\n")

    top_results = scored_results[:rag_top_k]

    if args.json:
        output = {
            "query": args.query,
            "mode": mode,
            "total_entries": len(entries),
            "results": [
                {
                    "rank": i + 1,
                    "score": round(score, 6),
                    "text": entry.get("text", ""),
                    "source": entry.get("source", ""),
                    "category": entry.get("category", ""),
                    "id": entry.get("id", ""),
                    "access_count": entry.get("access_count", 0),
                    "age_cycles": entry.get("age_cycles", 0),
                    "created_at": entry.get("created_at", ""),
                    "last_access": entry.get("last_access", "")
                }
                for i, (score, entry) in enumerate(top_results)
            ]
        }

        print(json.dumps(output, indent=2, ensure_ascii=False))
        sys.exit(0)

    # Formatierte Ausgabe
    print(f"{'=' * 60}")
    print(f"  🎯 Ergebnisse: {len(top_results)} von {len(scored_results)} Treffern (min_score={args.min_score})")
    print(f"{'=' * 60}")

    if not top_results:
        print("\n❌ Keine Ergebnisse gefunden.")
        print("     Mögliche Ursachen:")
        print("     - Query passt nicht zum gespeicherten Wissen")
        print("     - Embedding-Modus ist 'hash' (kein semantisches Modell)")
        print("     - min_score zu hoch gesetzt")
        sys.exit(0)

    for i, (score, entry) in enumerate(top_results):
        print(format_entry(entry, score, i, verbose=args.verbose))

    # Zusammenfassung
    print(f"\n{'=' * 60}")

    best_score = top_results[0][0] if top_results else 0
    avg_score = sum(s for s, _ in top_results) / len(top_results) if top_results else 0

    print(f"  📈 Bester Score:  {best_score:.4f}")
    print(f"  📊 Ø Score:       {avg_score:.4f}")
    print(f"  📋 Angezeigt:     {len(top_results)} / {len(scored_results)} Treffer")

    if mode == "hash":
        print("\n⚠️  HINWEIS: Hash-Embeddings sind nur lexikalisch.")
        print("     Für semantische Suche: sentence-transformers/lokales Modell prüfen.")

    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
