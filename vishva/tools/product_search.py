"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
import re
import json
import time
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple
try:
    import requests
except ImportError:
    requests = None

def _product_search(self, args: Dict[str, Any]) -> Dict[str, Any]:
    query = (args.get("query", "") or "").strip()
    if not query:
        return {"error": "query is required."}

    sources_arg = args.get("sources")
    if sources_arg is None:
        sources = ["idealo", "geizhals", "ddg"]
    elif isinstance(sources_arg, str):
        sources = [s.strip() for s in sources_arg.split(",") if s.strip()]
    elif isinstance(sources_arg, list):
        sources = sources_arg
    else:
        sources = ["idealo", "geizhals", "ddg"]
    try:
        max_results = min(int(args.get("max_results", 5)), 15)
    except (TypeError, ValueError):
        max_results = 5
    all_results = []
    errors = []
    source_stats = {}
    source_map = {
        "idealo": self._search_idealo,
        "geizhals": self._search_geizhals,
        "ddg": self._search_ddg_shopping,
        "duckduckgo": self._search_ddg_shopping,}
    for src in sources:
        src_lower = str(src).lower()
        fn = source_map.get(src_lower)
        if not fn:
            errors.append(f"{src}: unknown source (use idealo/geizhals/ddg)")
            continue
        try:
            results = fn(query, max_results)
            source_stats[src_lower] = len(results)
            for r in results:
                r["source"] = src_lower
            all_results.extend(results)
        except Exception as e:
            errors.append(f"{src}: {type(e).__name__}: {e}")
            source_stats[src_lower] = 0
    seen_titles = set()
    unique = []
    for r in all_results:
        title_key = re.sub(r'\s+', ' ', r.get("title", "").lower().strip())
        if title_key and title_key not in seen_titles:
            seen_titles.add(title_key)
            unique.append(r)
    def price_key(item):
        p = item.get("price", "")
        if isinstance(p, str):
            nums = re.findall(r'[\d.,]+', p.replace('.', '').replace(',', '.'))
            try:
                return float(nums[0]) if nums else 999999
            except Exception:
                return 999999
        return 999999
    unique.sort(key=price_key)
    query_lower = query.lower()
    query_words = set(re.findall(r'\b\w{3,}\b', query_lower))
    filtered = []
    for r in unique:
        title_lower = r.get("title", "").lower()
        url = r.get("url", "")
        if url in ("https://www.idealo.de", "https://geizhals.de"):
            continue
        if "/MainSearchProductCategory.html" in url:
            continue
        junk_markers = [
            "in suchanfragen", "beliebteste produkte", "in ssds",
            "in tablets", "in pcs", "preisvergleich für",
            "idealo - deutschlands", "geizhals - ",]
        if any(marker in title_lower for marker in junk_markers):
            continue
        if r.get("source") in ("idealo", "geizhals"):
            title_words = set(re.findall(r'\b\w{3,}\b', title_lower))
            if not title_words & query_words:
                continue
        filtered.append(r)
    seen_urls = set()
    deduped = []
    for r in filtered:
        url = r.get("url", "").rstrip("/")
        if url and url not in seen_urls:
            seen_urls.add(url)
            deduped.append(r)
    if len(deduped) > max_results * 3:
        deduped = deduped[:max_results * 3]

    return {
        "success": True,
        "query": query,
        "count": len(deduped),
        "results": deduped,
        "sources_tried": sources,
        "source_stats": source_stats,
        "errors": errors if errors else None,}
    if len(unique) > max_results * 3:
        unique = unique[:max_results * 3]
    return {
        "success": True,
        "query": query,
        "count": len(unique),
        "results": unique,
        "sources_tried": sources,
        "source_stats": source_stats,
        "errors": errors if errors else None,}

def _search_idealo(self, query: str, max_results: int) -> List[Dict[str, Any]]:
    url = (f"https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?q={query}")

    def consent_and_scroll(pg):
        for sel in ["button:has-text('Akzeptieren')",
                    "button:has-text('Alle akzeptieren')",
                    "[id*='accept']", "[class*='accept']"]:
            try:
                btn = pg.locator(sel).first
                if btn.is_visible(timeout=1500):
                    btn.click()
                    pg.wait_for_timeout(1500)
                    break
            except Exception:
                continue
        try:
            pg.mouse.wheel(0, 1200)
            pg.wait_for_timeout(1500)
            pg.mouse.wheel(0, 1200)
            pg.wait_for_timeout(1000)
        except Exception:
            pass
    for engine in ("stealth", "dynamic"):
        try:
            if engine == "stealth":
                from scrapling.fetchers import StealthyFetcher
                page = StealthyFetcher.fetch(
                    url, headless=True, network_idle=True,
                    page_action=consent_and_scroll, timeout=45000)
            else:
                from scrapling.fetchers import DynamicFetcher
                page = DynamicFetcher.fetch(
                    url, headless=True, network_idle=True,
                    disable_resources=False,
                    page_action=consent_and_scroll, timeout=45000)
        except ImportError:
            continue
        except Exception as e:
            if self.config.get("debug", 0) > 1:
                print(f"[Idealo {engine}] fetch error: {type(e).__name__}: {e}")
            continue
        if self.config.get("debug", 0) > 1:
            print(f"[Idealo {engine}] status={page.status}")
        if page.status != 200:
            continue
        results = self._idealo_extract(page, max_results)
        if self.config.get("debug", 0) > 1:
            print(f"[Idealo {engine}] extracted {len(results)} results")
        if results:
            return results
    return self._search_idealo_requests(query, max_results)

def _idealo_extract(self, page, max_results: int) -> List[Dict[str, Any]]:
    results = []
    cards = page.css('article, [class*="productCard"], [class*="product-card"], [data-testid*="product"]')
    for card in cards[:max_results * 2]:
        title = (card.css('h2::text, h3::text, [class*="title"]::text').get() or "").strip()
        item_text = " ".join(card.css('::text').getall())
        m = re.search(r'(\d{1,3}(?:\.\d{3})*,\d{2})\s*€', item_text)
        price = m.group(0).strip() if m else ""
        link = ""
        for a in card.css('a'):
            href = (a.css('::attr(href)').get() or "").strip()
            if href and not href.startswith(("javascript:", "#", "/preisvergleich/MainSearch", "/preisvergleich/OffersOfProduct")):
                if "/preisvergleich/" in href or href.startswith("http"):
                    link = href
                    break
        if link and not link.startswith("http"):
            link = "https://www.idealo.de" + link
        if title and link and len(title) > 15 and not title.startswith("Beliebteste"):
            results.append({"title": title[:200], "price": price, "url": link, "source": "idealo"})
        if len(results) >= max_results:
            break
    if results:
        return results
    seen = set()
    for a in page.css('a'):
        href = (a.css('::attr(href)').get() or "").strip()
        text = " ".join(a.css('::text').getall()).strip()
        if not href or href in seen or len(text) < 15:
            continue
        if href.startswith(("javascript:", "#", "/preisvergleich/MainSearch", "/preisvergleich/OffersOfProduct")):
            continue
        if "/preisvergleich/ProductCategory/" not in href and not href.startswith("http"):
            continue
        seen.add(href)
        if not href.startswith("http"):
            href = "https://www.idealo.de" + href
        price = ""
        parent = a.parent
        for _ in range(3):
            if parent is None:
                break
            parent_text = " ".join(parent.css('::text').getall() if hasattr(parent, 'css') else [])
            m = re.search(r'(\d{1,3}(?:\.\d{3})*,\d{2})\s*€', parent_text)
            if m:
                price = m.group(0).strip()
                break
            parent = parent.parent if hasattr(parent, 'parent') else None
        results.append({"title": text[:200], "price": price, "url": href, "source": "idealo"})
        if len(results) >= max_results:
            break
    return results

def _search_idealo_requests(self, query: str, max_results: int) -> List[Dict[str, Any]]:
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    url = (f"https://www.idealo.de/preisvergleich/"
           f"MainSearchProductCategory.html?q={requests.utils.quote(query)}")
    headers = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9",}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if self.config.get("debug", 0) > 1:
            print(f"[Idealo-requests] status={resp.status_code}, html={len(resp.text)} chars")
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(" ", strip=True)
            if "/preisvergleich/" not in href or href in seen:
                continue
            if len(text) < 15 or href.startswith(("javascript:", "#")):
                continue
            seen.add(href)
            if not href.startswith("http"):
                href = "https://www.idealo.de" + href
            price = ""
            parent = a.parent
            for _ in range(3):
                if parent is None:
                    break
                m = re.search(r'(\d{1,3}(?:\.\d{3})*,\d{2})\s*€',
                              parent.get_text(" ", strip=True))
                if m:
                    price = m.group(0).strip()
                    break
                parent = parent.parent
            results.append({"title": text[:200], "price": price, "url": href})
            if len(results) >= max_results:
                break
        return results
    except Exception as e:
        if self.config.get("debug", 0) > 1:
            print(f"[Idealo-requests error] {type(e).__name__}: {e}")
        return []

def _search_geizhals(self, query: str, max_results: int) -> List[Dict[str, Any]]:
    try:
        from scrapling.fetchers import StealthyFetcher
    except ImportError:
        print("[product_search] Geizhals: scrapling not installed, using requests fallback")
        return self._search_geizhals_requests(query, max_results)
    url = f"https://geizhals.de/?fs={query}&hloc=at&hloc=de"
    try:
        def action(pg):
            for sel in ["button:has-text('Akzeptieren')", "[id*='accept']"]:
                try:
                    btn = pg.locator(sel).first
                    if btn.is_visible(timeout=1500):
                        btn.click()
                        pg.wait_for_timeout(1500)
                        break
                except Exception:
                    continue
            try:
                pg.mouse.wheel(0, 600)
                pg.wait_for_timeout(1000)
            except Exception:
                pass
        page = StealthyFetcher.fetch(
            url, headless=True, network_idle=True,
            page_action=action, timeout=45000)
        if self.config.get("debug", 0) > 1:
            print(f"[Geizhals] status={page.status}")
        if page.status != 200:
            return self._search_geizhals_requests(query, max_results)
        results = []
        items = page.css('.listview__item, .productlist__item, article, [class*="product"], [data-testid*="product"]')
        if self.config.get("debug", 0) > 1:
            print(f"[Geizhals] found {len(items)} items")
        for item in items[:max_results]:
            title = (item.css('h3::text, h2::text, [class*="name"]::text').get() or "").strip()
            if not title:
                for a_el in item.css('a'):
                    txt = a_el.css('::text').get()
                    if txt and txt.strip() and len(txt.strip()) > 10:
                        title = txt.strip()
                        break
            item_text = " ".join(item.css('::text').getall())
            price_match = re.search(r'(\d{1,3}(?:\.\d{3})*,\d{2})\s*€', item_text)
            if not price_match:
                price_match = re.search(r'ab\s+(\d{1,3}(?:\.\d{3})*,\d{2})', item_text)
            if not price_match:
                price_match = re.search(r'€\s*(\d{1,3}(?:\.\d{3})*,\d{2})', item_text)
            if not price_match:
                price_match = re.search(r'Preis[:\s]+(\d{1,3}(?:\.\d{3})*,\d{2})', item_text, re.IGNORECASE)
            price = price_match.group(0).strip() if price_match else ""
            link = ""
            for a_el in item.css('a'):
                href = a_el.css('::attr(href)').get()
                if href and href.strip() and not href.startswith("javascript:"):
                    link = href.strip()
                    break
            if link and not link.startswith("http"):
                link = "https://geizhals.de" + link
            if not link and self.config.get("debug", 0) > 1:
                print(f"[Geizhals DEBUG] title='{title[:50]}', price='{price}', link=EMPTY")
                print(f"[Geizhals DEBUG] first 3 <a> hrefs: {[a.css('::attr(href)').get() for a in item.css('a')[:3]]}")
            if title and link:
                results.append({
                    "title": title[:200],
                    "price": price,
                    "url": link,
                    "source": "geizhals",})
        if self.config.get("debug", 0) > 1:
            print(f"[Geizhals] extracted {len(results)} results")
        return results
    except Exception as e:
        if self.config.get("debug", 0) > 1:
            print(f"[Geizhals error] {type(e).__name__}: {e}")
        return self._search_geizhals_requests(query, max_results)

def _search_geizhals_requests(self, query: str, max_results: int) -> List[Dict[str, Any]]:
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    url = f"https://geizhals.de/?fs={requests.utils.quote(query)}&hloc=at&hloc=de"
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if self.config.get("debug", 0) > 1:
            print(f"[Geizhals-requests] status={resp.status_code}")
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        items = soup.select('.listview__item, .productlist__item, article')
        for item in items[:max_results]:
            title_el = item.select_one('a, h3, h2')
            title = title_el.get_text(strip=True) if title_el else ""
            price_match = re.search(r'(\d{1,3}(?:\.\d{3})*,\d{2})\s*€', item.get_text())
            if not price_match:
                price_match = re.search(r'ab\s+(\d{1,3}(?:\.\d{3})*,\d{2})', item.get_text())
            price = price_match.group(0).strip() if price_match else ""
            link_el = item.select_one('a[href]')
            link = link_el.get("href", "") if link_el else ""
            if link and not link.startswith("http"):
                link = "https://geizhals.de" + link
            if title and link:
                results.append({"title": title[:200], "price": price, "url": link})
        return results
    except Exception as e:
        if self.config.get("debug", 0) > 1:
            print(f"[Geizhals-requests error] {type(e).__name__}: {e}")
        return []

def _search_ddg_shopping(self, query: str, max_results: int) -> List[Dict[str, Any]]:
    try:
        from ddgs import DDGS
    except ImportError:
        return []
    shopping_query = f"{query} kaufen Preis"
    try:
        with DDGS() as ddgs:
            raw = list(ddgs.text(shopping_query, region="de-de", max_results=max_results * 2))
        results = []
        for r in raw:
            title = (r.get("title") or "").strip()
            url = (r.get("href") or "").strip()
            snippet = (r.get("body") or "").strip()
            if not title or not url:
                continue
            combined_text = f"{title} {snippet}"
            price = ""
            price_match = re.search(r'(\d{1,3}(?:\.\d{3})*,\d{2})\s*€', combined_text)
            if not price_match:
                price_match = re.search(r'€\s*(\d{1,3}(?:\.\d{3})*,\d{2})', combined_text)
            if not price_match:
                price_match = re.search(r'ab\s+(\d{1,3}(?:\.\d{3})*,\d{2})\s*€', combined_text, re.IGNORECASE)
            if not price_match:
                price_match = re.search(r'ab\s+€\s*(\d{1,3}(?:\.\d{3})*,\d{2})', combined_text, re.IGNORECASE)
            if not price_match:
                price_match = re.search(r'[$€£]\s*(\d{1,3}(?:,\d{3})*\.\d{2})', combined_text)
            if not price_match:
                price_match = re.search(r'Preis[:\s]+(\d{1,3}(?:\.\d{3})*,\d{2})\s*€', combined_text, re.IGNORECASE)
            if price_match:
                full_match = price_match.group(0).strip()
                if '€' not in full_match and '£' not in full_match and '$' not in full_match:
                    full_match = f"{full_match} €"
                price = full_match
            if any(m in url for m in (
                    "duckduckgo.com/y.js", "ad_provider=",
                    "bing.com/aclick", "googleadservices")):
                continue
            if not any(kw in combined_text.lower()
                      for kw in ["€", "preis", "kaufen", "shop", "angebot", "ab "]):
                continue
            results.append({
                "title": title[:200],
                "price": price,
                "url": url,
                "snippet": snippet[:150],})

            if len(results) >= max_results:
                break
        if self.config.get("debug", 0) > 1:
            print(f"[DDG] web-search shopping: {len(results)} results")
        return results
    except Exception as e:
        if self.config.get("debug", 0) > 1:
            print(f"[DDG] web-search error: {type(e).__name__}: {e}")
        return []
