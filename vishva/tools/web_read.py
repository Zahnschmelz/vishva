"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
import re
import json
import time
#import shutil
#import subprocess
#from typing import Any, Dict, List, Optional, Tuple
from typing import Any, Dict, Optional

try:
    import requests
except ImportError:
    requests = None


def _web_read(self, args: Dict[str, Any]) -> Dict[str, Any]:
    url = args.get("url", "").strip()
    if not url:
        return {"error": "url is required."}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    max_chars = int(self.config.get("web_read_max_chars", 15000))
    if "max_chars" in args:
        max_chars = int(args.get("max_chars"))
    wait_for = args.get("wait_for", "")
    timeout_ms = int(args.get("timeout", 30)) * 1000
    take_screenshot = bool(args.get("screenshot", False))
    use_js = args.get("js_render", True)
    use_stealth = args.get("stealth", True)
    cache_fallback = args.get("cache_fallback", True)
    is_amazon = "amazon.de" in url or "amazon.com" in url or "amazon.co." in url
    if is_amazon and "/dp/" in url:
        return self._web_read_amazon_product(
            url, max_chars, timeout_ms, take_screenshot, use_stealth)
    is_ecommerce = any(domain in url for domain in [
        "otto.de", "zalando.", "aboutyou.",
        "mediamarkt.", "saturn.", "conrad.", "alternate.",
        "cyberport.", "notebooksbilliger.",])

    if use_js:
        try:
            from playwright.sync_api import sync_playwright
            from playwright.sync_api import TimeoutError as PlaywrightTimeout
            try:
                from playwright_stealth import stealth_sync
                has_stealth = True
            except ImportError:
                has_stealth = False
        except ImportError:
            return self._web_read_static(url, max_chars, timeout_ms // 1000)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-features=IsolateOrigins,site-per-process",
                        "--no-sandbox",])
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
                    viewport={"width": 1920, "height": 1080},
                    locale="de-DE",
                    timezone_id="Europe/Berlin",
                    extra_http_headers={
                        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",},)
                page = context.new_page()
                if use_stealth and has_stealth:
                    stealth_sync(page)
                elif use_stealth:
                    page.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                        window.chrome = { runtime: {} };""")

                def route_handler(route):
                    if route.request.resource_type in ["image", "font", "media"]:
                        route.abort()
                    else:
                        route.continue_()
                page.route("**/*", route_handler)

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    if is_ecommerce:
                        scrapling_result = self._web_read_ecommerce_scrapling(url, max_chars)
                        if scrapling_result.get("success") and scrapling_result.get("chars", 0) > 300:
                            return scrapling_result
                        else:
                            def ecom_action(pg):
                                consent_selectors = [
                                    "button:has-text('Akzeptieren')",
                                    "button:has-text('Alle akzeptieren')",
                                    "button:has-text('Zustimmen')",
                                    "button:has-text('Accept all')",
                                    "[data-testid='uc-accept-all-button']",
                                    ".uc-list-button__primary",
                                    "#usercentrics-root button:first-of-type",
                                    "button.consent-accept",
                                    "button[id*='accept']",
                                    "button[class*='accept']",]
                                for sel in consent_selectors:
                                    try:
                                        btn = pg.locator(sel).first
                                        if btn.is_visible(timeout=1500):
                                            btn.click()
                                            pg.wait_for_timeout(1500)
                                            break
                                    except Exception:
                                        continue
                                try:
                                    pg.mouse.wheel(0, 800)
                                    pg.wait_for_timeout(1000)
                                    pg.mouse.wheel(0, -400)
                                    pg.wait_for_timeout(500)
                                except Exception:
                                    pass
                            ecom_action(page)
                    if wait_for:
                        page.wait_for_selector(wait_for, timeout=timeout_ms)
                    elif is_ecommerce:
                        product_selectors = [
                            "[data-testid='productTitle']",
                            "h1.product-title",
                            ".product-title",
                            "#productTitle",
                            "h1",
                            "[itemprop='name']",]
                        for sel in product_selectors:
                            try:
                                page.wait_for_selector(sel, timeout=5000)
                                break
                            except Exception:
                                continue
                    else:
                        page.wait_for_timeout(2500)
                    if is_ecommerce:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                        page.wait_for_timeout(1000)
                        page.evaluate("window.scrollTo(0, 0)")
                        page.wait_for_timeout(500)
                    try:
                        body_text = page.inner_text("body")[:1000].lower()
                    except Exception:
                        body_text = ""
                    bot_markers = [
                        "klicke auf die schaltfl",
                        "click the button below",
                        "captcha", "robot check", "are you a human",
                        "automated access", "blocked", "access denied",
                        "just a moment", "checking your browser",
                        "enable javascript", "cloudflare",
                        "seite wurde nicht gefunden",
                        "seite nicht gefunden",
                        "page not found",]
                    bot_detected = any(m in body_text for m in bot_markers)
                    content_empty = len(body_text.strip()) < 100
                    title = page.title() or ""
                    page_not_found = any(x in title.lower() for x in
                                         ["nicht gefunden", "not found", "404", "seite existiert nicht"])
                    if page_not_found:
                        browser.close()
                        return {
                            "success": False,
                            "error": f"Page not found (404): {url}",
                            "title": title,
                            "method": "playwright_stealth",}
                    if (bot_detected or content_empty) and cache_fallback:
                        browser.close()
                        cached = self._web_read_cached(url, max_chars)
                        if cached:
                            cached["warning"] = (
                                "Original page blocked/empty — "
                                "served from web archive")
                            return cached
                        browser2 = p.chromium.launch(headless=True)
                        page2 = browser2.new_page()
                        page2.goto(url, wait_until="domcontentloaded",
                                   timeout=timeout_ms)
                        page2.wait_for_timeout(1500)
                        html2 = page2.content()
                        title2 = page2.title() or "(blocked)"
                        browser2.close()
                        result = self._extract_content_from_html(
                            html2, url, title2, max_chars, "")
                        result["warning"] = (
                            "Bot-Detection/block detected, "
                            "all archive fallbacks failed")
                        result["method"] = "playwright_blocked"
                        return result
                    title = page.title() or "(no title)"
                    html = page.content()
                    screenshot_path = ""
                    if take_screenshot:
                        screenshot_path = os.path.join(
                            self.workdir,
                            f"screenshot_{int(time.time())}.png")
                        page.screenshot(path=screenshot_path, full_page=True)
                    browser.close()
                    result = self._extract_content_from_html(
                        html, url, title, max_chars, screenshot_path)
                    result["method"] = "playwright_stealth" if use_stealth else "playwright"
                    return result
                except PlaywrightTimeout:
                    browser.close()
                    return {"error": f"Timeout after {timeout_ms // 1000}s loading {url}"}
                except Exception as e:
                    browser.close()
                    return {"error": f"Playwright error: {type(e).__name__}: {e}"}
        except Exception as e:
            return self._web_read_static(url, max_chars, timeout_ms // 1000)
    return self._web_read_static(url, max_chars, timeout_ms // 1000)



def _web_read_ecommerce_scrapling(self, url: str, max_chars: int) -> Dict[str, Any]:
    try:
        from scrapling.fetchers import StealthyFetcher
    except ImportError:
        return {"success": False, "error": "scrapling not installed"}
    def ecom_action(pg):
        consent_selectors = [
            "button:has-text('Akzeptieren')",
            "button:has-text('Alle akzeptieren')",
            "button:has-text('Zustimmen')",
            "button:has-text('Accept all')",
            "[data-testid='uc-accept-all-button']",
            "[id*='accept']",
            "[class*='accept']",]
        for sel in consent_selectors:
            try:
                btn = pg.locator(sel).first
                if btn.is_visible(timeout=1500):
                    btn.click()
                    pg.wait_for_timeout(1500)
                    break
            except Exception:
                continue
        try:
            pg.mouse.wheel(0, 800)
            pg.wait_for_timeout(1000)
            pg.mouse.wheel(0, -400)
            pg.wait_for_timeout(500)
        except Exception:
            pass
    try:
        page = StealthyFetcher.fetch(
            url, headless=True, network_idle=True,
            page_action=ecom_action, timeout=60000)
        if page.status != 200:
            return {"success": False, "error": f"HTTP {page.status}"}
        markdown = page.markdown()
        if len(markdown) > max_chars:
            markdown = markdown[:max_chars] + "\n\n[... truncated]"
        return {
            "success": True,
            "url": url,
            "title": page.css('title::text').get() or "(no title)",
            "content": markdown,
            "chars": len(markdown),
            "method": "scrapling_ecommerce",
            "status": page.status,}
    except Exception as e:
        return {"success": False, "error": f"Scrapling error: {type(e).__name__}: {e}"}



def _web_read_cached(self, url: str, max_chars: int) -> Optional[Dict[str, Any]]:
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8",}

    def _extract(html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer",
                         "header", "aside"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    try:
        cache_url = (
            f"https://webcache.googleusercontent.com/search"
            f"?q=cache:{url}&hl=de&gl=de")
        resp = requests.get(cache_url, headers=headers, timeout=15)
        if resp.status_code == 200 and len(resp.text) > 500:
            text = _extract(resp.text)
            if len(text) > 200:
                if len(text) > max_chars:
                    text = text[:max_chars] + "\n\n[... truncated]"
                return {
                    "success": True,
                    "url": url,
                    "title": f"(Google Cache of {url[:60]})",
                    "content": text,
                    "chars": len(text),
                    "method": "cache_google",}
    except Exception:
        pass
    try:
        wb_api = f"https://archive.org/wayback/available?url={url}"
        api_resp = requests.get(wb_api, headers=headers, timeout=10)
        if api_resp.status_code == 200:
            data = api_resp.json()
            snap_url = (data.get("archived_snapshots", {}).get("closest", {}).get("url", ""))
            if snap_url:
                page = requests.get(snap_url, headers=headers, timeout=20)
                if page.status_code == 200:
                    text = _extract(page.text)
                    if len(text) > 200:
                        if len(text) > max_chars:
                            text = text[:max_chars] + "\n\n[... truncated]"
                        return {
                            "success": True,
                            "url": url,
                            "title": f"(Wayback: {snap_url[:80]})",
                            "content": text,
                            "chars": len(text),
                            "method": "cache_wayback",
                            "snapshot_url": snap_url,}
    except Exception:
        pass
    try:
        at_url = f"https://archive.ph/newest/{url}"
        page = requests.get(at_url, headers=headers, timeout=15, allow_redirects=True)
        if page.status_code == 200 and "archive.ph" in page.url:
            text = _extract(page.text)
            if len(text) > 200:
                if len(text) > max_chars:
                    text = text[:max_chars] + "\n\n[... truncated]"
                return {
                    "success": True,
                    "url": url,
                    "title": f"(archive.ph of {url[:60]})",
                    "content": text,
                    "chars": len(text),
                    "method": "cache_archiveph",
                    "snapshot_url": page.url,}
    except Exception:
        pass
    return None



def _web_read_static(self, url: str, max_chars: int, timeout: int) -> Dict[str, Any]:
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        return {"error": "Neither Playwright nor requests+bs4 available."}
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.string if soup.title else "(no title)"
        return self._extract_content_from_html(html, url, title, max_chars, "")
    except Exception as e:
        return {"error": f"Static fetch failed: {type(e).__name__}: {e}"}



def _web_read_amazon_product(self, url: str, max_chars: int, timeout_ms: int,
                              take_screenshot: bool, use_stealth: bool) -> Dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        try:
            from playwright_stealth import stealth_sync
            has_stealth = True
        except ImportError:
            has_stealth = False
    except ImportError:
        return {"error": "Playwright not installed. Run: pip install playwright && playwright install chromium"}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="de-DE",
                timezone_id="Europe/Berlin",)
            page = context.new_page()
            if use_stealth and has_stealth:
                stealth_sync(page)
            page.add_init_script("""
                // Amazon-Modals ausblenden
                const observer = new MutationObserver(() => {
                    document.querySelectorAll('[data-feature-id="prime-upsell"], .a-modal, .a-popover').forEach(el => {
                        if (el.style) el.style.display = 'none';});});
                observer.observe(document.body, {childList: true, subtree: true});""")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_selector("#productTitle, #title, .product-title", timeout=10000)
                page.wait_for_timeout(3000)
                page.evaluate("""
                    () => {
                        // Typische Amazon-Overlays
                        const selectors = [
                            '.a-modal', '.a-popover', '[data-feature-id="prime-upsell"]',
                            '#attachSi498498_attachSiDisplay', '.a-overlay'];
                        selectors.forEach(sel => {
                            document.querySelectorAll(sel).forEach(el => el.remove());});}""")
                product_data = page.evaluate("""
                    () => {
                        const getText = (sel) => {
                            const el = document.querySelector(sel);
                            return el ? el.innerText.trim() : '';};
                        const getAll = (sel) => {
                            return Array.from(document.querySelectorAll(sel))
                                .map(el => el.innerText.trim())
                                .filter(t => t);};
                        const title = getText('#productTitle') || getText('#title') || getText('.product-title');
                        const price = getText('.a-price-whole') + getText('.a-price-fraction') ||
                                     getText('#priceblock_ourprice') || getText('.a-price');
                        const rating = getText('#acrPopover') || getText('.a-icon-alt');
                        const reviewCount = getText('#acrCustomerReviewText');
                        // Feature-Bullets
                        const features = getAll('#feature-bullets ul li span.a-list-item');
                        // Produktbeschreibung
                        const description = getText('#productDescription') || getText('#productDescription_feature_div');
                        // Technische Details
                        const techDetails = getAll('#productDetails_techSpec_section_1 tr, #detailBullets_feature_div li');
                        // Top-Reviews
                        const reviews = Array.from(document.querySelectorAll('[data-hook="review"]')).slice(0, 3).map(r => {
                            const title = r.querySelector('[data-hook="review-title"]')?.innerText.trim() || '';
                            const rating = r.querySelector('[data-hook="review-star-rating"]')?.innerText.trim() || '';
                            const body = r.querySelector('[data-hook="review-body"]')?.innerText.trim() || '';
                            return {title, rating, body: body.substring(0, 300)};});
                        return {title, price, rating, reviewCount, features, description, techDetails, reviews};}""")
                title = page.title() or "(no title)"
                html = page.content()
                screenshot_path = ""
                if take_screenshot:
                    screenshot_path = os.path.join(
                        self.workdir, f"screenshot_{int(time.time())}.png")
                    page.screenshot(path=screenshot_path, full_page=True)
                browser.close()
                md_parts = []
                if product_data.get("title"):
                    md_parts.append(f"# {product_data['title']}\n")
                if product_data.get("price"):
                    md_parts.append(f"**Preis:** {product_data['price']}\n")
                if product_data.get("rating"):
                    md_parts.append(f"**Bewertung:** {product_data['rating']}")
                    if product_data.get("reviewCount"):
                        md_parts[-1] += f" ({product_data['reviewCount']})"
                    md_parts.append("")
                if product_data.get("features"):
                    md_parts.append("## Hauptmerkmale\n")
                    for feat in product_data["features"][:10]:
                        if feat and not feat.startswith("{"):
                            md_parts.append(f"- {feat}")
                    md_parts.append("")
                if product_data.get("description"):
                    md_parts.append("## Produktbeschreibung\n")
                    md_parts.append(product_data["description"][:2000])
                    md_parts.append("")
                if product_data.get("techDetails"):
                    md_parts.append("## Technische Details\n")
                    for detail in product_data["techDetails"][:15]:
                        md_parts.append(f"- {detail}")
                    md_parts.append("")
                if product_data.get("reviews"):
                    md_parts.append("## Top-Rezensionen\n")
                    for i, review in enumerate(product_data["reviews"], 1):
                        md_parts.append(f"### Rezension {i}: {review['title']}\n")
                        md_parts.append(f"**{review['rating']}**\n")
                        md_parts.append(review["body"])
                        md_parts.append("")
                content = "\n".join(md_parts)
                if len(content) > max_chars:
                    content = content[:max_chars] + "\n\n[... truncated]"
                result = {
                    "success": True,
                    "url": url,
                    "title": title,
                    "content": content,
                    "chars": len(content),
                    "method": "amazon_scraper",
                    "product_data": product_data,}
                if screenshot_path:
                    result["screenshot"] = screenshot_path
                return result
            except PlaywrightTimeout:
                browser.close()
                return {"error": f"Timeout loading Amazon product page: {url}"}
            except Exception as e:
                browser.close()
                return {"error": f"Amazon scraper error: {type(e).__name__}: {e}"}
    except Exception as e:
        return {"error": f"Playwright error: {type(e).__name__}: {e}"}



def _extract_content_from_html(
    self, html: str, url: str, title: str, max_chars: int, screenshot_path: str) -> Dict[str, Any]:
    try:
        from readability import Document
        from markdownify import markdownify as md
    except ImportError:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[... truncated]"
        result = {
            "success": True,
            "url": url,
            "title": title,
            "content": text,
            "chars": len(text),
            "method": "fallback_bs4",}
        if screenshot_path:
            result["screenshot"] = screenshot_path
        return result
    doc = Document(html)
    main_html = doc.summary()
    title = doc.short_title() or title
    markdown = md(main_html, heading_style="ATX", strip=["img", "script", "style"])
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    if len(markdown) > max_chars:
        markdown = markdown[:max_chars] + "\n\n[... truncated]"
    result = {
        "success": True,
        "url": url,
        "title": title,
        "content": markdown,
        "chars": len(markdown),
        "method": "playwright" if screenshot_path or "playwright" in str(type(self)) else "readability",}
    if screenshot_path:
        result["screenshot"] = screenshot_path
    return result

