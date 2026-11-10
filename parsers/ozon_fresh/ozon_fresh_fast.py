"""Public Ozon Fresh catalog runner. No account login, challenge solving or proxy rotation.

The transport contract must be calibrated against an anonymous public capture.
See README_FAST.md; no location or warehouse endpoints are guessed here.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import http.cookiejar
import json
import math
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

MSK = timezone(timedelta(hours=3))
COLUMNS = "service h3_index parsed_at store_id delivery_eta_min delivery_fee min_order product_id name brand manufacturer country category price price_regular price_card discount_percent promo amount unit_price unit_price_basis in_stock stock_qty rating reviews_count kcal protein fat carbs composition description shelf_life storage_conditions labels image_url url extra".split()
REQUIRED = "service h3_index parsed_at product_id name price price_regular amount".split()
NUMERIC = "delivery_eta_min delivery_fee min_order price price_regular price_card discount_percent unit_price stock_qty rating reviews_count kcal protein fat carbs".split()
HOST = "https://www.ozon.ru"
API = HOST + "/api/entrypoint-api.bx/page/json/v2?url="
BLOCK_WORDS = ("antibot challenge", "доступ ограничен", "подтвердите, что вы не робот", "captcha challenge")
AMOUNT_UNIT = r"(?:кг|мг|гр\.?|г|мл|л|штук(?:а|и)?|шт\.?|уп\.?|порц\.?|капсул(?:а|ы)?|таблет(?:ка|ки|ок)|стик(?:а|ов)?|пакетик(?:а|ов)?|пакет(?:а|ов)?|саше)"
AMOUNT = re.compile(r"\d+(?:[.,]\d+)?\s*(?:(?:шт\.?\s*)?[×xх]\s*\d+(?:[.,]\d+)?\s*)?"+AMOUNT_UNIT+r"(?!\w)", re.I)
SECRET = re.compile(r"token|cookie|authorization|password|phone|email|session|fingerprint|tracking", re.I)


class ParseError(RuntimeError):
    pass


class ProtectionError(ParseError):
    pass


class BudgetError(ParseError):
    pass


def now():
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")


def observed_now():
    """Receipt time for ordering concurrently fetched observations."""
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S.%f")


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data, best_effort=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # Windows readers/antivirus can briefly hold the destination without
    # FILE_SHARE_DELETE. A status reader must not terminate a catalog crawl.
    for attempt in range(6):
        try:
            temp.replace(path)
            return True
        except PermissionError:
            if attempt==5:
                if best_effort:return False
                raise
            time.sleep(.05*(2**attempt))


def pointer(obj, path, default=None):
    """RFC 6901 pointer; decode only the JSON strings encountered along this path."""
    if path is None:
        return default
    if path == "":
        return obj
    if not path.startswith("/"):
        raise ParseError(f"JSON pointer must start with '/': {path}")
    try:
        for part in path[1:].split("/"):
            if isinstance(obj, str):
                obj = json.loads(obj)
            part = part.replace("~1", "/").replace("~0", "~")
            obj = obj[int(part)] if isinstance(obj, list) else obj[part]
        return obj
    except (KeyError, IndexError, TypeError, ValueError):
        return default


def number(value):
    return _number(value,12)


def nutrition_number(value):
    """Preserve published nutrition decimals, including float serialization tails."""
    return _number(value,32)


def field_number(field,value):
    return nutrition_number(value) if field in ('kcal','protein','fat','carbs') else number(value)


def _number(value,max_fraction_digits):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ParseError("Boolean in a numeric field")
    text = re.sub(r"[\s\u2009\u202f\u00a0]", "", str(value)).replace(",", ".")
    text = re.sub(r"(?:₽|руб\.?|%)$", "", text, flags=re.I)
    try:
        result = Decimal(text)
    except InvalidOperation:
        raise ParseError(f"Invalid number: {value!r}") from None
    if not result.is_finite() or result < 0 or result.adjusted() > 12 or result.as_tuple().exponent < -max_fraction_digits:
        raise ParseError(f"Invalid nonnegative number: {value!r}")
    return format(result, "f")


def truth(value):
    if value is True or str(value).strip().lower() in ("true", "1"):
        return True
    if value is False or str(value).strip().lower() in ("false", "0"):
        return False
    raise ParseError(f"Expected explicit boolean, got {value!r}")


def load_zones(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        zones = list(csv.DictReader(f))
    seen = set()
    active, skipped = [], []
    for zone in zones:
        zid = zone.get("h3_index", "")
        if not re.fullmatch(r"87[0-9a-f]{13}", zid) or zid in seen:
            raise ParseError(f"Invalid or duplicate resolution-7 zone: {zid!r}")
        seen.add(zid)
        if not truth(zone.get("address_in_zone")):
            skipped.append(zone)
            continue
        if not zone.get("address", "").strip():
            raise ParseError(f"Missing house address: {zid}")
        try:
            lat, lon = float(zone["address_lat"]), float(zone["address_lon"])
        except (ValueError, KeyError):
            raise ParseError(f"Missing house coordinates: {zid}") from None
        if not (55 < lat < 57 and 36 < lon < 39):
            raise ParseError(f"House coordinates outside Moscow: {zid}")
        active.append(zone)
    return active, skipped


def render(obj, values):
    """Typed substitution; strings with embedded placeholders are URL encoded."""
    if isinstance(obj, dict):
        return {k: render(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render(v, values) for v in obj]
    if not isinstance(obj, str):
        return obj
    match = re.fullmatch(r"\{([a-z_]+)\}", obj)
    if match:
        if match[1] not in values:
            raise ParseError(f"Unknown placeholder: {match[1]}")
        return values[match[1]]
    def sub(match):
        if match[1] not in values:
            raise ParseError(f"Unknown placeholder: {match[1]}")
        return urllib.parse.quote(str(values[match[1]]), safe="")
    return re.sub(r"\{([a-z_]+)\}", sub, obj)


def public_url(url):
    parts = urllib.parse.urlsplit(urllib.parse.urljoin(HOST, url))
    if parts.scheme != "https" or parts.hostname not in ("ozon.ru", "www.ozon.ru") or parts.username or parts.password or parts.port not in (None, 443):
        raise ParseError("Only HTTPS public ozon.ru endpoints are allowed")
    if parts.fragment:
        raise ParseError("Fragments are not allowed in request URLs")
    return urllib.parse.urlunsplit(parts)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, seconds=7200, interval=1.05, max_requests=6800, cookies=None):
        if interval < 1:
            raise ParseError("Request interval must be at least 1 second")
        self.deadline = time.monotonic() + seconds
        self.interval, self.max_requests = interval, max_requests
        self.last, self.count = None, 0
        self.blocked = False
        jar = http.cookiejar.MozillaCookieJar()
        if cookies:
            jar.load(str(cookies), ignore_discard=True, ignore_expires=False)
            if any(c.name.lower() in ("__secure-access-token", "__secure-refresh-token", "access_token", "refresh_token") for c in jar):
                raise ParseError("Account cookies are prohibited. Use an anonymous capture.")
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(jar))

    def check(self):
        if self.blocked:
            raise ProtectionError("Transport stopped after protection response")
        if self.count >= self.max_requests or time.monotonic() >= self.deadline:
            raise BudgetError("Time/request budget exhausted; progress preserved")

    def request(self, spec, values=None):
        self.check()
        spec = render(spec, values or {})
        url = public_url(spec["url"])
        method = spec.get("method", "GET").upper()
        if method not in ("GET", "POST"):
            raise ParseError("Only recorded GET/POST public storefront requests are allowed")
        if not re.match(r"^/(?:api/|category/|modal/)", urllib.parse.urlsplit(url).path):
            raise ParseError("Endpoint is outside the storefront request paths")
        headers = {"Accept": "application/json", "User-Agent": "OzonFreshResearch/1.0"}
        for key, value in spec.get("headers", {}).items():
            if key.lower() not in ("accept", "content-type", "origin", "referer", "x-o3-app-name", "x-o3-app-version"):
                raise ParseError(f"Unsupported header in public contract: {key}")
            headers[key] = value
        data = None
        if "json" in spec:
            data = json.dumps(spec["json"], ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if method == "GET" and data is not None:
            raise ParseError("GET request with a body")
        if self.last is not None:
            delay = self.last + self.interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        self.check()
        timeout = min(30, self.deadline - time.monotonic())
        self.last = time.monotonic()
        self.count += 1
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in (401, 403, 429) or 300 <= exc.code < 400:
                self.blocked = True
                raise ProtectionError(f"HTTP {exc.code}; stopped without retry. Increase pause and check public access.") from None
            raise ParseError(f"HTTP {exc.code}; zone remains unfinished") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ParseError(f"Network error ({type(exc).__name__}); zone remains unfinished") from None
        if len(raw) > 32 * 1024 * 1024:
            raise ParseError("JSON response exceeds 32 MiB")
        text = raw.decode("utf-8-sig")
        if "json" not in content_type.lower():
            if any(word in text[:20000].lower() for word in BLOCK_WORDS):
                self.blocked = True
                raise ProtectionError("Challenge page; stopped without retry")
            raise ParseError("Expected JSON; HTML is not an empty catalog")
        try:
            result = json.loads(text)
        except ValueError:
            raise ParseError("Invalid JSON; zone remains unfinished") from None
        if not isinstance(result, dict):
            raise ParseError("Expected JSON object")
        for key, value in result.get("widgetStates", {}).items():
            if key.startswith("profileMenu") and not key.startswith("profileMenuAnonymous"):
                raise ProtectionError("Authenticated profile detected; only anonymous storefront is permitted")
        return result


def distance(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(math.radians, map(float, (lat1, lon1, lat2, lon2)))
    a = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(a)))


def read_context(data, schema, zone):
    available = truth(pointer(data, schema["delivery_available"]))
    lat, lon = pointer(data, schema["lat"]), pointer(data, schema["lon"])
    try:
        delta = distance(lat, lon, zone["address_lat"], zone["address_lon"])
    except (TypeError, ValueError):
        raise ParseError("Cannot verify delivery house coordinates") from None
    if not math.isfinite(delta) or delta > 100:
        raise ParseError(f"Address switch not confirmed: {delta:.0f} m from requested house")
    store = pointer(data, schema.get("store_id"))
    if store is not None and (isinstance(store, (list, dict, bool)) or not str(store).strip()):
        raise ParseError("Invalid Fresh warehouse ID")
    result = {"store_id": str(store) if store is not None else "", "delivery_available": available}
    for field in ("delivery_eta_min", "delivery_fee", "min_order"):
        result[field] = number(pointer(data, schema.get(field)))
    # This must be an actual server scope field, not a hash of the visible product sample.
    result["price_scope"] = str(pointer(data, schema.get("price_scope"), ""))
    return result


def card_row(card, mapping, category):
    row = {field: pointer(card, path) for field, path in mapping.items() if field in COLUMNS}
    extra = {field: pointer(card, path) for field, path in mapping.items() if field not in COLUMNS}
    if row.get("extra"):
        parsed_extra = json.loads(row["extra"]) if isinstance(row["extra"], str) else row["extra"]
        if not isinstance(parsed_extra, dict):
            raise ParseError("Card extra must be a JSON object")
        extra = dict(parsed_extra, **extra)
    row["extra"] = json.dumps(extra, ensure_ascii=False)
    row["category"] = category["name"]
    if row.get("url"):
        row["url"] = public_url(row["url"])
    if not row.get("product_id") and row.get("url"):
        match = re.search(r"/product/(?:[^/?]*-)?(\d+)/?(?:\?|$)", row["url"])
        if match:
            row["product_id"] = match[1]
    if row.get("product_id") is not None:
        row["product_id"] = str(row["product_id"])
    if not row.get("amount") and isinstance(row.get("name"), str):
        matches = AMOUNT.findall(row["name"])
        if matches:
            row["amount"] = matches[-1].strip()
    for key in NUMERIC:
        if key in row:
            row[key] = field_number(key,row[key])
    if row.get("price") is None:
        raise ParseError("Card has no explicit ordinary purchase price; card price cannot replace it")
    if row.get("price_regular") is None:
        row["price_regular"] = row["price"]
    if Decimal(row["price_regular"]) < Decimal(row["price"]):
        raise ParseError("Regular price is lower than purchase price; check price mapping")
    if row.get("in_stock") is not None:
        row["in_stock"] = "true" if truth(row["in_stock"]) else "false"
    if isinstance(row.get("labels"), list):
        row["labels"] = "; ".join(str(x) for x in row["labels"])
    if any(isinstance(v, (dict, list)) for v in row.values()):
        raise ParseError("Unmapped structured value in a CSV field")
    for key in ("product_id", "name", "amount"):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise ParseError(f"Card missing required field: {key}")
    if not re.fullmatch(r"\d+", row["product_id"]):
        raise ParseError("Non-numeric Ozon product ID")
    if row.get("url"):
        match = re.search(r"/product/(?:[^/?]*-)?(\d+)/?(?:\?|$)", row["url"])
        if not match or match[1] != row["product_id"]:
            raise ParseError("Product URL and ID disagree")
    return row


def catalog_page(data, schema, request_url=None):
    """Primary catalog only: never recursively mine recommendations for prices."""
    source = data
    if schema.get('primary_search_results'):
        from ozon_har_catalog import primary_catalog_grid
        source=primary_catalog_grid(data,require_layout=True,request_url=request_url)
    elif "widget_prefix" in schema:
        widgets = data.get("widgetStates", {})
        candidates = [v for k, v in widgets.items() if k.startswith(schema["widget_prefix"])]
        if len(candidates) != 1:
            raise ParseError(f"Expected one primary catalog widget, found {len(candidates)}")
        source = candidates[0]
        if isinstance(source, str):
            source = json.loads(source)
    items = pointer(source, schema["items"])
    if not isinstance(items, list):
        raise ParseError("Catalog items path changed; refusing to export an incomplete zone")
    # Null/empty next pointer is valid ONLY when its path exists. Missing != last page.
    sentinel = object()
    next_page = pointer(data if schema.get("next_from_root", True) else source, schema["next"], sentinel)
    if next_page is sentinel and schema.get("pagination_widget_prefix"):
        candidates = [v for k,v in data.get("widgetStates", {}).items() if k.startswith(schema["pagination_widget_prefix"])]
        if len(candidates) == 1:
            paginator = json.loads(candidates[0]) if isinstance(candidates[0], str) else candidates[0]
            next_page = pointer(paginator, schema.get("pagination_widget_next", "/nextPage"), sentinel)
    if next_page is sentinel:
        raise ParseError("Pagination end is unverified: next-page field is missing")
    if next_page is not None and not isinstance(next_page, str):
        raise ParseError("Next page must be a server-provided URL or explicit null")
    return items, next_page


def check_contract(config):
    if config.get("verified_anonymous_contract") is not True:
        raise ParseError("Live contract is not verified. Capture two anonymous delivery addresses first; see README_FAST.md")
    if config.get("fresh_only") is not True:
        raise ParseError("Contract must select the Fresh assortment, including Fresh delivery filters")
    for name in ("set_location", "context", "context_schema", "catalog_schema", "card_mapping", "categories"):
        if not config.get(name):
            raise ParseError(f"Missing verified contract section: {name}")
    for name in ("set_location", "context"):
        public_url(config[name]["url"])
    for key in ("lat", "lon", "delivery_available"):
        if not config["context_schema"].get(key):
            raise ParseError(f"Missing context field: {key}")
    for field in ("name", "price"):
        if not config["card_mapping"].get(field):
            raise ParseError(f"Missing card mapping: {field}")
    if not config["card_mapping"].get("product_id") and not config["card_mapping"].get("url"):
        raise ParseError("Card mapping must include product ID or URL")
    if "next" not in config["catalog_schema"]:
        raise ParseError("Missing pagination contract")
    seen = set()
    for category in config["categories"]:
        if category.get("scope") not in ("food", "household_chemicals") or not category.get("name"):
            raise ParseError("Only verified food and household chemical categories are allowed")
        if not category.get("category_id") or not category.get("verified_leaf"):
            raise ParseError("Each category needs a verified taxonomy ID and leaf flag")
        url = public_url(category["url"])
        if category["category_id"] in seen:
            raise ParseError("Duplicate category ID")
        seen.add(category["category_id"])
        if "/category/" not in url or "miniapp=supermarket" not in url:
            raise ParseError("Category must be explicitly scoped to the Fresh miniapp")


class State:
    def __init__(self, path, fingerprint):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS zones(h3 TEXT PRIMARY KEY,status TEXT NOT NULL,parsed_at TEXT,payload TEXT,error TEXT);
            CREATE TABLE IF NOT EXISTS catalogs(cache_key TEXT PRIMARY KEY,captured_at TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pages(cache_key TEXT NOT NULL,url TEXT NOT NULL,captured_at TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(cache_key,url));
            CREATE TABLE IF NOT EXISTS metrics(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS parse_failures(captured_at TEXT NOT NULL,cache_key TEXT NOT NULL,url TEXT NOT NULL,product_id TEXT,error TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS quarantined(h3 TEXT NOT NULL,category_id TEXT NOT NULL,product_id TEXT NOT NULL,captured_at TEXT NOT NULL,error TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(h3,category_id,product_id));
        """)
        old = self.db.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
        if old and old[0] != fingerprint:
            self.db.close()
            raise ParseError("Inputs or contract changed. Use a new state file for this snapshot")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('fingerprint',?)", (fingerprint,))
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('started_at',?)", (now(),))

    def started_at(self):
        return self.db.execute("SELECT value FROM meta WHERE key='started_at'").fetchone()[0]

    def save_parse_failure(self, cache_key, url, product_id, error, payload):
        # Preserve the failed public card locally so a format error can be
        # reproduced without repeating catalog or product requests.
        with self.db:
            self.db.execute('INSERT INTO parse_failures VALUES(?,?,?,?,?,?)',
                (now(),cache_key,url,str(product_id or ''),str(error),json.dumps(payload,ensure_ascii=False)))

    def mark(self, zid, status, payload=None, error=None):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO zones VALUES(?,?,?,?,?)", (zid, status, now(), json.dumps(payload, ensure_ascii=False) if payload is not None else None, error))

    def done(self, zid):
        row = self.db.execute("SELECT status FROM zones WHERE h3=?", (zid,)).fetchone()
        return bool(row and row[0] in ("complete", "collected_with_gaps", "no_delivery"))

    def quarantine(self, house, category, product, error):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO quarantined VALUES(?,?,?,?,?,?)',
                (house,str(category),str(product['product_id']),now(),str(error),json.dumps(product,ensure_ascii=False)))

    def quarantined_for(self, house):
        return [dict(product_id=sku,category_id=category,error=error,product=json.loads(payload))
                for sku,category,error,payload in self.db.execute('SELECT product_id,category_id,error,payload FROM quarantined WHERE h3=?',(house,))]

    def get_catalog(self, key):
        row = self.db.execute("SELECT captured_at,payload FROM catalogs WHERE cache_key=?", (key,)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def put_catalog(self, key, captured_at, rows):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO catalogs VALUES(?,?,?)", (key, captured_at, json.dumps(rows, ensure_ascii=False)))

    def get_page(self, key, url):
        row = self.db.execute("SELECT captured_at,payload FROM pages WHERE cache_key=? AND url=?", (key, url)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def put_page(self, key, url, captured_at, rows, next_page,confirmed_empty_primary=False):
        payload={"rows":rows,"next":next_page}
        if confirmed_empty_primary:payload['confirmed_empty_primary']=True
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO pages VALUES(?,?,?,?)", (key, url, captured_at, json.dumps(payload, ensure_ascii=False)))

    def export(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        count = 0
        with temp.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="raise")
            writer.writeheader()
            for zid, status, parsed_at, payload in self.db.execute("SELECT h3,status,parsed_at,payload FROM zones WHERE status IN ('complete','collected_with_gaps','no_delivery') ORDER BY h3"):
                if status == "no_delivery":
                    writer.writerow(dict(service="ozon_fresh", h3_index=zid, parsed_at=parsed_at))
                    count += 1
                    continue
                for product in json.loads(payload):
                    row = dict(product, service="ozon_fresh", h3_index=zid, parsed_at=product.get('parsed_at') or parsed_at)
                    writer.writerow(row)
                    count += 1
        temp.replace(path)
        return count


def collect_catalog(client, config, context, state=None, cache_key="", enrich_product=None,enrich_products=None,progress=None,catalog_index=None,page_pool=None):
    products, rejects = {}, []
    captured_at = now()
    from concurrent.futures import Future
    from ozon_catalog_scheduler import CatalogScheduler
    def sequential_request(request):
        future = Future()
        try:
            client.check()
            future.set_result({'data':client.request(request),'captured_at':observed_now()})
        except Exception as exc:
            future.set_exception(exc)
        return future
    pages = CatalogScheduler(config["categories"],state,cache_key,
        page_pool.request_async if page_pool else sequential_request,
        workers=len(page_pool.threads) if page_pool else 1)
    confirmed_empty_chains=set()
    previous_catalog_pages={}
    try:
        for task in pages:
            client.check()
            category,page_index,current_url = task.category,task.page_index,task.url
            page_key,cached_page = task.page_key,task.cached_page
            if cached_page:
                page_time, page = cached_page
                if page.get('confirmed_empty_primary'):confirmed_empty_chains.add(page_key)
                captured_at = min(captured_at, page_time)
                for row in page["rows"]:
                    if catalog_index: catalog_index.remember_product(cache_key, row)
                    previous = products.get(row["product_id"])
                    if previous and catalog_index:
                        from ozon_catalog_index import merge_observation
                        products[row['product_id']] = merge_observation(previous, row)
                    elif previous and any(previous.get(k) != row.get(k) for k in ("price", "price_regular", "price_card", "in_stock", "amount")):
                        raise ParseError("Cached duplicate offers disagree")
                    products.setdefault(row["product_id"], row)
                url = page["next"]
                previous_catalog_pages[page_key]=(current_url,url)
                if progress:progress(category,page_index,len(products),True)
                pages.complete(url)
                continue
            data = task.data
            page_observed_at = task.captured_at
            # If the API exposes warehouse/location context on every page, verify it here too.
            if config.get("catalog_store_pointer"):
                store = pointer(data, config["catalog_store_pointer"])
                if str(store) != context["store_id"]:
                    raise ParseError("Warehouse changed while collecting catalog")
            if config.get('card_decoder') == 'ozon_tile_v2' and context.get('verified_address'):
                from ozon_har_catalog import address_title, normalized_address
                page_address=address_title(data)
                if page_address is not None and normalized_address(page_address)!=normalized_address(context['verified_address']):
                    raise ParseError('Catalog page address differs from the confirmed house')
            if config.get('card_decoder')=='ozon_tile_v2' and page_index>1:
                from ozon_har_catalog import empty_catalog_tail,empty_separator_continuation
                tail=empty_catalog_tail(data,current_url,
                    confirmed_empty_parent=page_key in confirmed_empty_chains,
                    expected_coordinates=context.get('verified_coordinates'),expected_category_url=category['url'])
                separator=empty_separator_continuation(data,current_url,
                    previous_page=previous_catalog_pages.get(page_key),
                    expected_coordinates=context.get('verified_coordinates'),expected_category_url=category['url'],
                    expected_address=context.get('verified_address'))
            else:tail=separator=False
            try:
                items, url = (([],None) if tail else ([],data['nextPage']) if separator else
                              catalog_page(data, config["catalog_schema"],request_url=current_url))
            except ParseError as exc:
                if state:
                    state.save_parse_failure(page_key,current_url,None,exc,
                        {'catalog_response':data,'page_index':page_index})
                raise
            confirmed_empty_primary=separator
            if not tail and not separator and not items and config.get('card_decoder')=='ozon_tile_v2':
                from ozon_har_catalog import primary_catalog_grid
                confirmed_empty_primary=primary_catalog_grid(data,require_layout=True,
                    request_url=current_url).get('confirmed_empty_category',False)
            if confirmed_empty_primary:confirmed_empty_chains.add(page_key)
            page_rows = []
            decoded=[]
            for item in items:
                # A verified seller/Fresh flag is mandatory when the category can contain marketplace goods.
                fresh_path = config["catalog_schema"].get("fresh_item_pointer")
                if fresh_path and pointer(item, fresh_path) != config["catalog_schema"].get("fresh_item_value", True):
                    continue
                try:
                    if config.get("card_decoder") == "ozon_tile_v2":
                        from ozon_har_catalog import tile_product
                        item = tile_product(item)
                except ParseError as exc:
                    rejects.append({"category_id": category["category_id"], "product_id": str(item.get("product_id") or item.get("sku") or ""), "reason": str(exc)})
                    if state:state.save_parse_failure(page_key,current_url,item.get('product_id') or item.get('sku'),exc,{'raw_card':item})
                    continue
                extra = item.get('extra') or {}
                if isinstance(extra,str):extra=json.loads(extra)
                extra['catalog_quote_observed_at'] = page_observed_at
                item['extra'] = extra
                decoded.append(item)
            if catalog_index:
                from ozon_catalog_index import needs_product
                decoded = [catalog_index.resolve(cache_key, item, products) for item in decoded]
            else:
                needs_product = lambda item: item.get('price') is None or item.get('unit_price_basis')
            if enrich_products:
                pending=[];positions=[]
                for position,item in enumerate(decoded):
                    if needs_product(item):
                        known=products.get(item['product_id'])
                        if not catalog_index and known and not item.get('unit_price_basis') and known.get('price') is not None and known.get('price_card')==item.get('price_card'):
                            if item.get('price_regular') is not None and known.get('price_regular')!=item['price_regular']:
                                raise ParseError('Catalog and product original prices disagree')
                            decoded[position]=dict(item,price=known['price'],price_regular=known['price_regular'])
                        else:pending.append(item);positions.append(position)
                if pending:
                    enriched=enrich_products(pending)
                    if len(enriched)!=len(pending):raise ParseError('Price batch has missing items')
                    for position,item in zip(positions,enriched):decoded[position]=item
            for item in decoded:
                if item.get('_quarantine_reason') and config.get('quarantine_incomplete'):
                    if not state:raise ParseError('Quarantine requires durable state')
                    state.quarantine(cache_key,category['category_id'],item,item['_quarantine_reason'])
                    continue
                # Network/protection errors in enrichment must stop the run immediately.
                if enrich_product and needs_product(item):
                    known = products.get(item["product_id"])
                    if not catalog_index and known and not item.get('unit_price_basis') and known.get("price") is not None and known.get("price_card") == item.get("price_card"):
                        if item.get("price_regular") is not None and known.get("price_regular") != item["price_regular"]:
                            raise ParseError("Catalog and product original prices disagree")
                        # The same offer can occur in several categories for this
                        # house. Reuse only within this catalog, never across zones.
                        item = dict(item, price=known["price"], price_regular=known["price_regular"])
                    else:
                        try:item = enrich_product(item)
                        except ParseError as exc:
                            from ozon_product_prices import MissingAmountError
                            from ozon_product_redirect import UnavailableProduct
                            if not config.get('quarantine_incomplete') or not isinstance(exc,(MissingAmountError,UnavailableProduct)):raise
                            if not state:raise ParseError('Quarantine requires durable state')
                            state.quarantine(cache_key,category['category_id'],exc.item,str(exc))
                            continue
                try:
                    row = card_row(item, config["card_mapping"], category)
                    extra = item.get('extra') or {}
                    if isinstance(extra,str):extra=json.loads(extra)
                    received_at=(extra.get('ordinary_price_observed_at')
                        if extra.get('ordinary_price_source')=='product_webPrice' else None) or page_observed_at
                    extra['observation_received_at']=received_at
                    row['parsed_at']=received_at[:19]
                    row['extra']=json.dumps(extra,ensure_ascii=False)
                except ParseError as exc:
                    if config.get('quarantine_incomplete') and 'missing required field: amount' in str(exc):
                        if not state:raise ParseError('Quarantine requires durable state')
                        state.quarantine(cache_key,category['category_id'],item,str(exc))
                        continue
                    rejects.append({"category_id": category["category_id"], "product_id": str(item.get("product_id") or item.get("sku") or ""), "reason": str(exc)})
                    if state:
                        raw=next((card for card in items if str(card.get('sku') or card.get('product_id'))==str(item.get('product_id'))),{})
                        state.save_parse_failure(page_key,current_url,item.get('product_id'),exc,{'decoded':item,'raw_card':raw})
                    continue
                if catalog_index: catalog_index.remember_product(cache_key, row)
                previous = products.get(row["product_id"])
                if catalog_index:
                    from ozon_catalog_index import merge_observation
                    products[row['product_id']] = merge_observation(previous, row)
                elif previous and any(previous.get(k) != row.get(k) for k in ("price", "price_regular", "price_card", "in_stock", "amount")):
                    raise ParseError(f"Inconsistent offer for product {row['product_id']}; cannot silently choose a price")
                products.setdefault(row["product_id"], row)
                page_rows.append(row)
            if rejects:
                raise ParseError(f"{len(rejects)} cards missing required fields; no partial zone exported. First: {rejects[0]}")
            if state:
                if confirmed_empty_primary:
                    state.put_page(page_key,current_url,now(),page_rows,url,confirmed_empty_primary=True)
                else:state.put_page(page_key, current_url, now(), page_rows, url)
            if progress:progress(category,page_index,len(products),False)
            previous_catalog_pages[page_key]=(current_url,url)
            pages.complete(url)
    except BaseException as original:
        if page_pool:page_pool.stop_event.set()
        try:
            pages.drain()
        except Exception as pending_error:
            if isinstance(original,BudgetError) and not isinstance(pending_error,BudgetError):
                raise pending_error from original
        raise
    else:
        pages.drain()
    if rejects:
        raise ParseError(f"{len(rejects)} cards missing required fields; no partial zone exported. First: {rejects[0]}")
    if not products and not (config.get('quarantine_incomplete') and state and state.quarantined_for(cache_key)):
        raise ParseError("Available warehouse returned zero target products; this is not proof of no delivery")
    return captured_at, list(products.values())


def run(args):
    config = load_json(args.contract)
    check_contract(config)
    zones, skipped = load_zones(args.zones)
    if args.limit:
        zones = zones[:args.limit]
    input_bytes = Path(args.contract).read_bytes() + Path(args.zones).read_bytes()
    state_path = Path(args.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = State(state_path, hashlib.sha256(input_bytes).hexdigest())
    started = datetime.strptime(state.started_at(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK)
    elapsed = (datetime.now(MSK) - started).total_seconds()
    if elapsed >= args.seconds:
        raise BudgetError("Snapshot time budget expired. Use a new state file for a fresh collection")
    used = state.db.execute("SELECT value FROM metrics WHERE key='requests'").fetchone()
    remaining_requests = args.max_requests - (used[0] if used else 0)
    if remaining_requests <= 0:
        raise BudgetError("Snapshot request budget exhausted")
    client = Client(args.seconds - elapsed, args.interval, remaining_requests, args.cookies)
    reason, exit_code = None, 0
    rows = 0
    t0 = time.monotonic()
    try:
        for i, zone in enumerate(zones, 1):
            zid = zone["h3_index"]
            if state.done(zid):
                continue
            print(f"[{i}/{len(zones)}] {zid}: setting delivery house", flush=True)
            try:
                values = dict(zone, lat=float(zone["address_lat"]), lon=float(zone["address_lon"]))
                client.request(config["set_location"], values)
                raw_context = client.request(config["context"], values)
                context = read_context(raw_context, config["context_schema"], zone)
                if not context["delivery_available"]:
                    state.mark(zid, "no_delivery")
                    continue
                # Without an explicit price scope, reuse is disabled by default.
                reuse = bool(context["store_id"]) and (bool(context["price_scope"]) or config.get("warehouse_defines_full_offer") is True)
                key = json.dumps([context["store_id"], context["price_scope"], "" if reuse else zid])
                cached = state.get_catalog(key)
                if cached:
                    captured_at, products = cached
                else:
                    captured_at, products = collect_catalog(client, config, context, state, key)
                    # Confirm the selected house and warehouse again after a multi-page crawl.
                    confirm = read_context(client.request(config["context"], values), config["context_schema"], zone)
                    if any(confirm[k] != context[k] for k in ("store_id", "price_scope", "delivery_available")):
                        raise ParseError("Delivery context changed during collection")
                    context = confirm
                    state.put_catalog(key, captured_at, products)
                rows = []
                for product in products:
                    extra = json.loads(product.get("extra") or "{}")
                    extra.update({"catalog_captured_at": captured_at, "offer_reused": bool(cached), "delivery_address": zone["address"], "category_scope": "verified_food_or_household_chemicals"})
                    row = dict(product, **{k: context[k] for k in ("store_id", "delivery_eta_min", "delivery_fee", "min_order")})
                    row["extra"] = json.dumps(extra, ensure_ascii=False)
                    rows.append(row)
                state.mark(zid, "complete", rows)
                print(f"  {len(rows)} products; warehouse {context['store_id']}; {'cache' if cached else 'new catalog'}; requests {client.count}", flush=True)
            except (ProtectionError, BudgetError):
                raise
            except ParseError as exc:
                state.mark(zid, "error", error=str(exc))
                # Contract drift/address failures must stop the run instead of wasting 197 zones.
                raise
    except (ParseError, KeyboardInterrupt) as exc:
        reason = str(exc) or "Interrupted by user"
        exit_code = 2
        print(reason, file=sys.stderr)
    finally:
        with state.db:
            state.db.execute("INSERT INTO metrics VALUES('requests',?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value", (client.count,))
        count = state.export(args.out)
        statuses = dict(state.db.execute("SELECT status,count(*) FROM zones GROUP BY status"))
        complete = all(state.done(z["h3_index"]) for z in zones)
        report = {"complete": complete, "requested_zones": len(zones), "skipped_no_house": len(skipped), "statuses": statuses, "exported_rows": count, "requests_this_invocation": client.count, "wall_seconds_this_invocation": round(time.monotonic()-t0, 2), "snapshot_started_at_msk": state.started_at(), "stop_reason": reason, "csv": str(Path(args.out).resolve()), "live_contract_verified_by_operator": True}
        write_json(Path(args.out).with_suffix(".report.json"), report)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        state.db.close()
    return exit_code


def plan(args):
    zones, skipped = load_zones(args.zones)
    budget = args.seconds
    seconds = args.stores * args.pages * args.interval + len(zones) * 2 * args.interval + args.stores * args.interval
    report = {"zones_total": len(zones)+len(skipped), "zones_with_house": len(zones), "excluded_no_house": [z["h3_index"] for z in skipped], "budget_seconds": budget, "scenario_only_not_a_benchmark": True, "assumed_unique_warehouses": args.stores, "assumed_catalog_pages_per_warehouse": args.pages, "minimum_request_seconds_excluding_latency": round(seconds, 2), "remaining_for_latency_and_export_seconds": round(budget-seconds, 2), "requests_per_second_max": 1/args.interval, "note": "Two location/context requests per zone; one post-crawl confirmation per warehouse. Reuse requires verified warehouse offer scope. Actual elapsed time requires a live 3-5-zone pilot."}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.out:
        write_json(args.out, report)
    return 0


def validate(args):
    zones, _ = load_zones(args.zones)
    expected = {z["h3_index"] for z in zones}
    errors, seen, covered, no_delivery = [], set(), set(), set()
    with Path(args.csv).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != COLUMNS:
            raise ParseError("CSV header differs from the 37-column template")
        count = 0
        for line, row in enumerate(reader, 2):
            count += 1
            if None in row or any(value is None for value in row.values()):
                errors.append(f"line {line}: malformed CSV row")
                continue
            zid = row["h3_index"]
            if zid not in expected or row["service"] != "ozon_fresh":
                errors.append(f"line {line}: invalid service or zone")
            try:
                datetime.strptime(row["parsed_at"], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                errors.append(f"line {line}: invalid Moscow timestamp")
            covered.add(zid)
            if not row["product_id"]:
                if any(v for k,v in row.items() if k not in ("service", "h3_index", "parsed_at")) or zid in no_delivery:
                    errors.append(f"line {line}: invalid no-delivery row")
                no_delivery.add(zid)
                continue
            key = (zid, row["product_id"])
            if key in seen:
                errors.append(f"line {line}: duplicate zone/product")
            seen.add(key)
            if any(not row[k] for k in REQUIRED):
                errors.append(f"line {line}: missing required field")
            for field in NUMERIC:
                try:
                    if row[field] and field_number(field,row[field]) != row[field]:
                        errors.append(f"line {line}: {field} not in dot numeric format")
                except ParseError:
                    errors.append(f"line {line}: invalid {field}")
            try:
                if row["extra"] and not isinstance(json.loads(row["extra"]), dict):
                    raise ValueError
            except ValueError:
                errors.append(f"line {line}: invalid extra JSON object")
    mixed = no_delivery & {zid for zid, _ in seen}
    if mixed:
        errors.append("Zones contain both product and no-delivery rows")
    missing = sorted(expected-covered)
    report = {"valid_rows": not errors, "complete": not errors and not missing, "rows": count, "zones_covered": len(covered & expected), "zones_expected": len(expected), "missing_zones": missing, "errors": errors[:100], "errors_count": len(errors)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.out:
        write_json(args.out, report)
    return 0 if report["complete"] else 2


def scrub(obj):
    if isinstance(obj, dict):
        return {k: scrub(v) for k,v in obj.items() if not SECRET.search(k)}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        if obj.startswith(("{", "[")):
            try:
                return scrub(json.loads(obj))
            except ValueError:
                pass
    return obj


def inspect_har(args):
    """Summarize only public storefront JSON; never copy auth headers or cookies."""
    har = load_json(args.har)
    summary, samples = [], []
    for entry in har.get("log", {}).get("entries", []):
        request, response = entry.get("request", {}), entry.get("response", {})
        try:
            url = public_url(request.get("url", ""))
        except ParseError:
            continue
        if "/api/" not in urllib.parse.urlsplit(url).path:
            continue
        content = response.get("content", {})
        text = content.get("text", "")
        if content.get("encoding") == "base64":
            import base64
            text = base64.b64decode(text).decode("utf-8")
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        widgets = data.get("widgetStates", {})
        if any(k.startswith("profileMenu") and not k.startswith("profileMenuAnonymous") for k in widgets):
            raise ParseError("HAR contains an authenticated profile. Capture in Incognito without logging in")
        parsed = urllib.parse.urlsplit(url)
        # Query values can carry session IDs. The summary keeps only parameter names.
        safe_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        body = request.get("postData", {}).get("text", "")
        try:
            body = scrub(json.loads(body))
        except (TypeError, ValueError):
            body = None
        summary.append({"index": len(samples), "method": request.get("method"), "endpoint": safe_url, "query_keys": list(urllib.parse.parse_qs(parsed.query)), "status": response.get("status"), "body_shape": list(body) if isinstance(body, dict) else None, "widget_keys": list(widgets), "root_keys": list(data)})
        # Keep data local. Never export the full HAR or credentials in the deliverable.
        samples.append({"endpoint": safe_url, "request_json": body, "response": scrub(data)})
    write_json(args.out, {"summary": summary, "samples": samples, "warning": "Local public research captures; inspect before sharing. Contract still needs calibration against two selected houses."})
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="Offline request budget scenario, not a measured promise")
    p.add_argument("--zones", required=True)
    p.add_argument("--stores", type=int, default=20)
    p.add_argument("--pages", type=int, default=150)
    p.add_argument("--interval", type=float, default=1.05)
    p.add_argument("--seconds", type=int, default=7200)
    p.add_argument("--out")
    p.set_defaults(func=plan)
    p = sub.add_parser("run", help="Run a calibrated public contract")
    p.add_argument("--zones", required=True)
    p.add_argument("--contract", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--cookies", help="Optional anonymous Mozilla cookie jar; never account cookies")
    p.add_argument("--interval", type=float, default=1.05)
    p.add_argument("--seconds", type=int, default=7200)
    p.add_argument("--max-requests", type=int, default=6800)
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=run)
    p = sub.add_parser("validate", help="Check schema, duplicates and coverage of all inhabited zones")
    p.add_argument("--zones", required=True)
    p.add_argument("--csv", required=True)
    p.add_argument("--out")
    p.set_defaults(func=validate)
    p = sub.add_parser("inspect-har", help="Inspect a local anonymous HAR with JSON response content")
    p.add_argument("--har", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=inspect_har)
    args = parser.parse_args(argv)
    try:
        if getattr(args, "seconds", 1) <= 0 or getattr(args, "interval", 1) < 1:
            raise ParseError("Positive budget and interval >= 1 second required")
        return args.func(args)
    except (ParseError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.modules.setdefault("ozon_fresh_fast", sys.modules[__name__])
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
