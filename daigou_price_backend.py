from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import json
import re
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HTML_FILE = ROOT / "daigou_beyblade_mvp.html"
INDEX_FILE = ROOT / "index.html"
LOG_FILE = ROOT.parent / "work" / "price_backend_debug.log"

FX_TO_TWD = {
    "JPY": 0.205,
    "TWD": 1.0,
    "MYR": 6.55,
    "HKD": 4.15,
    "SGD": 24.2,
    "THB": 0.9,
    "KRW": 0.024,
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
        "price_patterns": [r"[¥￥]\s*([0-9,]{3,})"],
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
        "price_patterns": [r"[¥￥]\s*([0-9,]{3,})"],
    },
    {
        "region": "台灣",
        "name": "PChome",
        "currency": "TWD",
        "url": "https://ecshweb.pchome.com.tw/search/v3.3/all/results?q={q}&page=1&sort=sale/dc",
        "price_patterns": [r'"price"\s*:\s*([0-9]{2,})'],
    },
    {
        "region": "台灣",
        "name": "momo",
        "currency": "TWD",
        "min_price": 300,
        "url": "https://www.momoshop.com.tw/search/searchShop.jsp?keyword={q}",
        "price_patterns": [r"\$?\s*([0-9,]{3,})"],
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


def clean_price(value):
    value = value.replace(",", "").strip()
    value = re.sub(r"[^\d.]", "", value)
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def extract_prices(html, patterns, currency):
    prices = []
    minimum = MIN_PRICE.get(currency, 1)
    for pattern in patterns:
        for match in re.finditer(pattern, html, flags=re.IGNORECASE):
            price = clean_price(match.group(1))
            if price and price >= minimum:
                prices.append(price)
    return sorted(set(prices))


def title_from_html(html):
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    title = re.sub(r"&amp;", "&", title)
    return title[:120]


def search_prices(query, region):
    wanted = [s for s in SOURCES if region in ("all", "", s["region"])]
    results = []
    for source in wanted:
        url = source["url"].format(q=quote(query))
        started = time.time()
        item = {
            "region": source["region"],
            "source": source["name"],
            "currency": source["currency"],
            "url": url,
            "status": "ok",
            "price": None,
            "price_twd": None,
            "title": "",
            "note": "",
            "elapsed_ms": None,
        }
        try:
            html = fetch_text(url)
            item["title"] = title_from_html(html)
            prices = extract_prices(html, source["price_patterns"], source["currency"])
            if source.get("min_price"):
                prices = [price for price in prices if price >= source["min_price"]]
            if prices:
                item["price"] = prices[0]
                item["price_twd"] = round(prices[0] * FX_TO_TWD[source["currency"]])
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
        results.append(item)
    return results


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
