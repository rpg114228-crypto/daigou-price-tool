from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
HTML_FILE = ROOT / "daigou_beyblade_mvp.html"
INDEX_FILE = ROOT / "index.html"
LOG_FILE = ROOT.parent / "work" / "price_backend_debug.log"

FX_UPDATED_AT = "2026-06-18"  # 銀行牌告即期賣出參考，需定期更新
FX_TO_TWD = {
    "JPY": 0.1987,
    "TWD": 1.0,
    "MYR": 7.150,
    "HKD": 4.056,
    "SGD": 24.59,
    "THB": 0.986,
    "KRW": 0.0235,
}

MIN_PRICE = {
    "JPY": 300,
    "TWD": 100,
    "MYR": 10,
    "HKD": 20,
    "SGD": 5,
    "THB": 100,
    "KRW": 1000,
}

SOURCES = [
    {
        "region": "日本",
        "name": "Rakuten Japan",
        "currency": "JPY",
        "url": "https://search.rakuten.co.jp/search/mall/{q}/",
        "engine": "playwright",
        "parser": "rakuten_jsonld",
        "price_patterns": [],
    },
    {
        "region": "日本",
        "name": "Yahoo Shopping JP",
        "currency": "JPY",
        "url": "https://shopping.yahoo.co.jp/search?p={q}",
        "price_patterns": [r"[¥￥]\s*([0-9,]{3,})", r'"price"\s*:\s*"?([0-9]{3,})"?'],
    },
    {
        "region": "日本",
        "name": "Amazon JP",
        "currency": "JPY",
        "url": "https://www.amazon.co.jp/s?k={q}",
        "parser": "amazon_search",
        "price_patterns": [],
    },
    {
        "region": "台灣",
        "name": "PChome",
        "currency": "TWD",
        "url": "https://ecshweb.pchome.com.tw/search/v3.3/all/results?q={q}&page=1&sort=sale/dc",
        "display_url": "https://24h.pchome.com.tw/search/?q={q}",
        "parser": "pchome_json",
        "price_patterns": [r'"price"\s*:\s*([0-9]{2,})'],
    },
    {
        "region": "台灣",
        "name": "momo",
        "currency": "TWD",
        "min_price": 300,
        "url": "https://www.momoshop.com.tw/search/searchShop.jsp?keyword={q}",
        "parser": "momo_jsonld",
        "price_patterns": [],
    },
    {
        "region": "馬來西亞",
        "name": "Lazada MY",
        "currency": "MYR",
        "url": "https://www.lazada.com.my/catalog/?q={q}",
        "price_patterns": [r"RM\s*([0-9,.]{2,})"],
    },
    {
        "region": "新加坡",
        "name": "Lazada SG",
        "currency": "SGD",
        "url": "https://www.lazada.sg/catalog/?q={q}",
        "price_patterns": [r"S\$\s*([0-9,.]{2,})"],
    },
    {
        "region": "香港",
        "name": "HKTVmall",
        "currency": "HKD",
        "url": "https://www.hktvmall.com/hktv/zh/search_a?keyword={q}",
        "price_patterns": [r"HK\$?\s*([0-9,.]{2,})"],
    },
]


def fetch_text(url):
    req = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8,ja;q=0.7",
        },
    )
    with urlopen(req, timeout=12) as res:
        raw = res.read(900_000)
        charset = res.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="ignore")


def fetch_rendered(url, locale="ja-JP"):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(locale=locale)
            page.goto(url, timeout=20000, wait_until="networkidle")
            page.wait_for_timeout(1200)
            return page.content()
        finally:
            browser.close()


def fetch_source_html(source, url):
    if source.get("engine") == "playwright":
        return fetch_rendered(url, locale=source.get("locale", "ja-JP"))
    return fetch_text(url)


def clean_price(value):
    value = value.replace(",", "").strip()
    value = re.sub(r"[^\d.]", "", value)
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def extract_prices(html, patterns, currency, query=None):
    prices = []
    minimum = MIN_PRICE.get(currency, 1)
    for pattern in patterns:
        for match in re.finditer(pattern, html, flags=re.IGNORECASE):
            price = clean_price(match.group(1))
            if not price or price < minimum:
                continue
            if query:
                start = max(0, match.start() - 200)
                end = min(len(html), match.end() + 200)
                context = re.sub(r"<[^>]+>", " ", html[start:end])
                if not is_relevant(context, query) or is_excluded(context):
                    continue
            prices.append(price)
    return sorted(set(prices))


def title_from_html(html):
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    title = re.sub(r"&amp;", "&", title)
    return title[:120]


def query_tokens(query):
    raw = re.split(r"[\s　/|,，、()（）\[\]【】]+", query.lower())
    tokens = []
    for token in raw:
        token = token.strip()
        if len(token) < 2:
            continue
        if token in {"x", "jp", "the", "and"}:
            continue
        tokens.append(token)
    return tokens


def is_relevant(text, query):
    hay = (text or "").lower().replace("-", "")
    tokens = query_tokens(query)
    if not tokens:
        return True
    strong = [token for token in tokens if re.search(r"(bx|ux|cx)-?\d+", token, re.I)]
    weak = [token for token in tokens if token not in strong]
    if strong:
        code_hit = any(
            re.search(rf"(?<![a-z0-9]){re.escape(token.replace('-', ''))}(?![a-z0-9])", hay)
            for token in strong
        )
        if not code_hit:
            return False
        if weak:
            return any(token in hay for token in weak)
        return True
    return sum(1 for token in tokens if token in hay) >= min(2, len(tokens))


EXCLUDE_KEYWORDS = [
    "中古", "二手", "ジャンク", "junk", "訳あり", "わけあり", "破損品",
    "予約金", "訂金", "頭期", "保証金", "deposit only",
]


def is_excluded(text):
    hay = (text or "").lower()
    return any(keyword.lower() in hay for keyword in EXCLUDE_KEYWORDS)


def parse_pchome_json(text, query):
    data = json.loads(text)
    products = data.get("prods") or []
    candidates = []
    for product in products:
        name = product.get("name") or ""
        desc = product.get("describe") or ""
        if not is_relevant(f"{name} {desc}", query) or is_excluded(f"{name} {desc}"):
            continue
        price = product.get("price")
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        if price >= MIN_PRICE["TWD"]:
            candidates.append((price, name))
    candidates.sort(key=lambda item: item[0])
    return candidates


def parse_momo_jsonld(text, query):
    candidates = []
    for block in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', text, flags=re.DOTALL):
        try:
            data = json.loads(block.group(1))
        except json.JSONDecodeError:
            continue
        for node in data.get("@graph") or []:
            if node.get("@type") != "ItemList":
                continue
            for entry in node.get("itemListElement") or []:
                name = entry.get("name") or ""
                if not is_relevant(name, query) or is_excluded(name):
                    continue
                offers = entry.get("offers") or {}
                try:
                    price = float(offers.get("price"))
                except (TypeError, ValueError):
                    continue
                currency = offers.get("priceCurrency") or "TWD"
                if price < MIN_PRICE.get(currency, 1):
                    continue
                candidates.append((price, name, entry.get("url"), currency))
    candidates.sort(key=lambda item: item[0])
    return candidates


def parse_rakuten_jsonld(text, query):
    candidates = []
    for block in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', text, flags=re.DOTALL):
        try:
            data = json.loads(block.group(1))
        except json.JSONDecodeError:
            continue
        if data.get("@type") != "ItemList":
            continue
        for entry in data.get("itemListElement") or []:
            product = entry.get("item") or {}
            name = product.get("name") or ""
            if not is_relevant(name, query) or is_excluded(name):
                continue
            offers = product.get("offers") or {}
            try:
                price = float(offers.get("price"))
            except (TypeError, ValueError):
                continue
            currency = offers.get("priceCurrency") or "JPY"
            if price < MIN_PRICE.get(currency, 1):
                continue
            candidates.append((price, name, product.get("url"), currency))
    candidates.sort(key=lambda item: item[0])
    return candidates


def parse_amazon_results(text, query):
    candidates = []
    for block in text.split('data-component-type="s-search-result"')[1:]:
        title_match = re.search(r'<h2[^>]*aria-label="([^"]+)"', block)
        price_match = re.search(r'a-offscreen">\s*([^<]*)</span>', block)
        if not title_match or not price_match:
            continue
        title = title_match.group(1)
        if not is_relevant(title, query) or is_excluded(title):
            continue
        raw = price_match.group(1).strip()
        amount = clean_price(raw)
        if amount is None:
            continue
        currency = "TWD" if raw.upper().startswith("TWD") else "JPY"
        if amount < MIN_PRICE.get(currency, 1):
            continue
        candidates.append((amount * FX_TO_TWD[currency], amount, currency, title))
    candidates.sort(key=lambda item: item[0])
    return candidates


def search_one_source(source, query):
    url = source["url"].format(q=quote(query))
    display_url = source.get("display_url", source["url"]).format(q=quote(query))
    started = time.time()
    item = {
        "region": source["region"],
        "source": source["name"],
        "currency": source["currency"],
        "url": display_url,
        "status": "ok",
        "price": None,
        "price_twd": None,
        "title": "",
        "note": "",
        "elapsed_ms": None,
    }
    try:
        html = fetch_source_html(source, url)
        item["title"] = title_from_html(html)
        parser = source.get("parser")
        if parser == "pchome_json":
            structured = parse_pchome_json(html, query)
            prices = [price for price, _name in structured]
            if structured:
                item["title"] = structured[0][1]
        elif parser == "momo_jsonld":
            structured = parse_momo_jsonld(html, query)
            prices = []
            if structured:
                price, name, product_url, currency = structured[0]
                item["title"] = name
                item["currency"] = currency
                if product_url:
                    item["url"] = product_url
                prices = [price]
        elif parser == "rakuten_jsonld":
            structured = parse_rakuten_jsonld(html, query)
            prices = []
            if structured:
                price, name, product_url, currency = structured[0]
                item["title"] = name
                item["currency"] = currency
                if product_url:
                    item["url"] = product_url
                prices = [price]
        elif parser == "amazon_search":
            structured = parse_amazon_results(html, query)
            prices = []
            if structured:
                _twd, amount, currency, title = structured[0]
                item["title"] = title
                item["currency"] = currency
                prices = [amount]
        else:
            prices = extract_prices(html, source["price_patterns"], source["currency"], query=query)
        if source.get("min_price"):
            prices = [price for price in prices if price >= source["min_price"]]
        if prices:
            item["price"] = prices[0]
            item["price_twd"] = round(prices[0] * FX_TO_TWD[item["currency"]])
            item["note"] = f"抓到 {len(prices)} 個價格，先列最低候選價。"
        else:
            item["status"] = "no_price"
            item["note"] = "頁面可讀，但沒有解析到價格，可能是動態載入。"
    except HTTPError as exc:
        item["status"] = "blocked"
        item["note"] = f"HTTP {exc.code}，可能被擋或需要瀏覽器。"
    except URLError as exc:
        item["status"] = "error"
        item["note"] = f"連線失敗：{exc.reason}"
    except Exception as exc:
        item["status"] = "error"
        item["note"] = str(exc)
    item["elapsed_ms"] = round((time.time() - started) * 1000)
    return item


def search_prices(query, region):
    wanted = [s for s in SOURCES if region in ("all", "", s["region"])]
    if not wanted:
        return []
    # Playwright 的 sync API 不支援在同一個 driver 跨執行緒共用，
    # 所以這類來源直接在目前的請求執行緒依序處理，不丟進 ThreadPoolExecutor。
    rendered_sources = [s for s in wanted if s.get("engine") == "playwright"]
    fast_sources = [s for s in wanted if s.get("engine") != "playwright"]

    results_by_name = {}
    if fast_sources:
        with ThreadPoolExecutor(max_workers=len(fast_sources)) as pool:
            for item in pool.map(lambda source: search_one_source(source, query), fast_sources):
                results_by_name[item["source"]] = item
    for source in rendered_sources:
        item = search_one_source(source, query)
        results_by_name[item["source"]] = item

    return [results_by_name[s["name"]] for s in wanted]


def debug_log(message):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_json({"ok": True})

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            debug_log(f"GET {parsed.path} {parsed.query}")
            if parsed.path == "/":
                content = (INDEX_FILE if INDEX_FILE.exists() else HTML_FILE).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            if parsed.path == "/api/prices":
                params = parse_qs(parsed.query)
                query = (params.get("q") or [""])[0].strip()
                region = (params.get("region") or ["all"])[0]
                debug_log(f"PRICE_START query={query} region={region}")
                if not query:
                    self.send_json({"ok": False, "error": "missing q"}, 400)
                    return
                results = search_prices(query, region)
                debug_log(f"PRICE_DONE count={len(results)}")
                valid = [r for r in results if r["price_twd"] is not None]
                best = min(valid, key=lambda r: r["price_twd"]) if valid else None
                self.send_json({
                    "ok": True,
                    "query": query,
                    "region": region,
                    "best": best,
                    "results": results,
                    "fx_updated_at": FX_UPDATED_AT,
                })
                return
            self.send_json({"ok": False, "error": "not found"}, 404)
        except BaseException as exc:
            debug_log(f"FATAL {type(exc).__name__}: {exc}")
            try:
                self.send_json({"ok": False, "error": str(exc)}, 500)
            except Exception:
                pass

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8787), Handler)
    server.serve_forever()
