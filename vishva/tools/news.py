#!/usr/bin/env python3
import sys
import json
import os
import re

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
CONFIG_PATH = os.path.join(PROJECT_DIR, "config.json")

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def get_ddgs():
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
            return DDGS
        except ImportError:
            return None

def fetch_article_text(url, max_chars=1500):
    headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'}
    import requests
    try:
        downloaded = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        if downloaded.status_code != 200:
            return None
    except Exception:
        return None
    try:
        import trafilatura
        extracted = trafilatura.extract(downloaded.text, include_comments=False)
        if extracted and len(extracted) > 200:
            text = extracted
            if len(text) > max_chars:
                end = text.rfind('. ', max_chars - 400, max_chars)
                if end == -1:
                    end = max_chars
                text = text[:end + 1]
            return text
    except Exception:
        pass
    try:
        text = downloaded.text
        text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > max_chars:
            end = text.rfind('. ', max_chars - 400, max_chars)
            if end == -1:
                end = max_chars
            text = text[:end + 1]
        return text if len(text) > 100 else None
    except Exception:
        return None

def news_search(query, max_results=8):
    DDGS = get_ddgs()
    if DDGS is None:
        return {"error": "ddgs not installed"}
    try:
        with DDGS() as ddgs:
            results = list(ddgs.news(query, region="de-de", timelimit="w", max_results=max_results))
        if not results:
            with DDGS() as ddgs:
                results = list(ddgs.news(query, region="de-de", max_results=max_results))
        if not results:
            return {"success": True, "results": []}
        formatted = []
        for r in results:
            formatted.append({
                "title": r.get("title", "") or "",
                "url": r.get("url", "") or "",
                "date": (r.get("date", "") or "")[:10],
                "source": r.get("source", "") or "",
                "snippet": (r.get("body", "") or "")[:250]})
        return {"success": True, "results": formatted}
    except Exception as e:
        return {"error": f"Search failed: {type(e).__name__}: {str(e)}"}

def llm_summarize(context, config):
    base_url = config.get("base_url") or config.get("base_url", "http://127.0.0.1:8080/v1")
    api_key = config.get("api_key", "llama")
    model = config.get("model", "llama3.1")
    system_msg = (
        "Du bist ein Nachrichten-Redakteur. Lies die bereitgestellten Artikel-Texte. "
        "Extrahiere für jedes Thema die 3 wichtigsten konkreten Fakten, Ereignisse oder Entwicklungen. "
        "Schreibe WAS passiert ist, WER betroffen ist, WANN (Datum steht bei jedem Artikel) "
        "und WARUM es wichtig ist. Nenne keine Webseiten-Beschreibungen, sondern nur echte "
        "Inhalte aus den Texten. Format pro Thema:\n"
        "### Thema\n"
        "1. **[Titel](URL)** (Datum) – 2 Sätze mit konkreten Fakten.\n"
        "Kein Filler. Keine Meta-Beschreibungen.")

    try:
        import requests
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": f"Top 3 pro Thema:\n\n{context}"}],
            "stream": False,}

        for key in ("temperature", "top_p", "top_k", "seed"):
            if config.get(key) is not None:
                payload[key] = config[key]

        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"},
            json=payload,
            timeout=600)
        resp.raise_for_status()
        data = resp.json()
        summary = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not summary:
            raise ValueError("Empty LLM response")
        return {"success": True, "summary": summary}
    except Exception as e:
        return {"error": f"LLM failed: {type(e).__name__}: {str(e)}"}

def main():
    if len(sys.argv) < 1 + 1:
        print(json.dumps({"error": "Usage: news_digest.py '<topic1>' '<topic2>' ..."}, ensure_ascii=False))
        sys.exit(1)
    raw_topics = sys.argv[1:]
    config = load_config()
    if get_ddgs() is None:
        print(json.dumps({"error": "pip install ddgs"}, ensure_ascii=False))
        sys.exit(1)
    topics = [t.strip() for t in raw_topics if t.strip()]
    all_results = {}
    for topic in topics:
        res = news_search(topic, max_results=8)
        if res.get("success"):
            articles = res.get("results", [])
            for art in articles[:4]:
                content = fetch_article_text(art["url"], max_chars=1500)
                art["content"] = content or art["snippet"]
            all_results[topic] = articles
        else:
            print(json.dumps({"warning": f"Search failed: {topic}", "detail": res.get("error")}, ensure_ascii=False), file=sys.stderr)
            all_results[topic] = []
    if not any(all_results.values()):
        print(json.dumps({"error": "No results."}, ensure_ascii=False))
        sys.exit(1)
    context_lines = []
    for topic, results in all_results.items():
        context_lines.append(f"--- THEMA: {topic} ---")
        for i, r in enumerate(results[:4], 1):
            context_lines.append(f'Artikel {i}: {r.get("title", "")[:80]} (Datum: {r.get("date", "")})')
            context_lines.append(f'URL: {r.get("url", "")}')
            context_lines.append(f'Text: {(r.get("content", "") or "")[:1500]}')
            context_lines.append('')
        context_lines.append('')
    context = "\n".join(context_lines)
    llm_result = llm_summarize(context, config)
    if llm_result.get("success"):
        output = {
            "success": True,
            "summary": llm_result["summary"],
            "topics_searched": list(all_results.keys()),
            "total_articles_scanned": sum(len(v) for v in all_results.values())}
    else:
        output = {
            "success": True,
            "warning": llm_result.get("error", "LLM failed"),
            "raw_results": {k: [{"t": a["title"], "u": a["url"], "d": a.get("date", ""), "s": a["snippet"]} for a in v] for k, v in all_results.items()}}
    print(json.dumps(output, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
