"""Read-only full baseline and separately labelled, incomplete refresh samples."""
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import ozon_fresh_fast as core


def _row(raw, house, stamp=None):
    row = {key: raw.get(key, "") for key in core.COLUMNS}
    for key, value in (("service", "ozon_fresh"), ("h3_index", house)):
        if row[key] not in ("", None, value):
            raise core.ParseError("Refresh row has a different service or house")
        row[key] = value
    row["parsed_at"] = row["parsed_at"] or stamp
    if any(row[key] is None or str(row[key]) == "" for key in core.REQUIRED):
        raise core.ParseError("Refresh/baseline row lacks one of the eight mandatory fields")
    try:
        core.datetime.strptime(row["parsed_at"], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError) as exc:
        raise core.ParseError("Invalid refresh/baseline Moscow timestamp") from exc
    for key in core.NUMERIC:
        if row[key] not in (None, ""):
            row[key] = core.number(row[key])
    for key in row:
        if row[key] is None:
            row[key] = ""
    extra = row["extra"]
    if isinstance(extra, str):
        try:
            extra = json.loads(extra or "{}")
        except ValueError as exc:
            raise core.ParseError("Invalid refresh/baseline extra JSON") from exc
    if not isinstance(extra, dict):
        raise core.ParseError("Refresh/baseline extra must be a JSON object")
    row["extra"] = json.dumps(extra, ensure_ascii=False)
    row["product_id"] = str(row["product_id"])
    return row


class Baseline:
    def __init__(self, path, zones, fingerprint):
        source = Path(path).resolve()
        if source.is_dir():
            source = source / "snapshot.sqlite"
        self.identity = str(source)
        self.db = None
        if not source.is_file():
            raise core.ParseError("Complete baseline snapshot.sqlite is missing")
        try:
            # mode=ro reads committed WAL data; immutable would silently miss live commits.
            self.db = sqlite3.connect("file:" + source.as_posix() + "?mode=ro", uri=True)
            self.db.execute("PRAGMA query_only=ON")
            meta = dict(self.db.execute("SELECT key,value FROM meta"))
            if meta.get("fingerprint") != fingerprint:
                raise core.ParseError("Baseline fingerprint differs from the refresh inputs")
            wanted = [str(zone["h3_index"]) for zone in zones]
            if len(wanted) != len(set(wanted)):
                raise core.ParseError("Duplicate house in baseline input zones")
            statuses = dict(self.db.execute("SELECT h3,status FROM zones"))
            if set(statuses) != set(wanted):
                raise core.ParseError("Baseline must contain exactly all supplied houses")
            if any(status not in ("complete", "collected_with_gaps", "no_delivery")
                   for status in statuses.values()):
                raise core.ParseError("Baseline catalog walk is incomplete; refresh cannot start")
            self.statuses = statuses
            self.has_gaps = "collected_with_gaps" in statuses.values()
        except Exception:
            self.close()
            raise

    def house(self, h3):
        record = self.db.execute("SELECT status,parsed_at,payload FROM zones WHERE h3=?", (h3,)).fetchone()
        if record is None:
            raise core.ParseError("House is absent from the baseline")
        status, stamp, payload = record
        if status == "no_delivery":
            return []
        rows = json.loads(payload)
        if not isinstance(rows, list):
            raise core.ParseError("Baseline house payload must be a product list")
        normalized = [_row(row, h3, stamp) for row in rows]
        if len({row["product_id"] for row in normalized}) != len(normalized):
            raise core.ParseError("Baseline contains duplicate house/SKU observations")
        return normalized

    def category_pages(self, h3):
        counts = {}
        prefix = json.dumps([h3])[:-1] + ","
        for cache_key, url, payload in self.db.execute(
                "SELECT cache_key,url,payload FROM pages WHERE cache_key>=? AND cache_key<?",
                (prefix, prefix + "\uffff")):
            key = json.loads(cache_key)
            if not isinstance(key, list) or len(key) != 2 or key[0] != h3:
                continue
            category = str(key[1]); counts.setdefault(category, 1)
            query = parse_qs(urlsplit(url).query)
            if "url" in query and urlsplit(url).path.startswith("/api/entrypoint-api."):
                query = parse_qs(urlsplit(query["url"][0]).query)
            if query.get("sold_out_page") or not json.loads(payload).get("rows"):
                continue
            try:
                page = int(query.get("page", ["1"])[0])
            except ValueError:
                continue
            counts[category] = max(counts[category], page)
        return counts

    def skus(self,h3):
        """Coverage export needs identifiers, not revalidation of every old field."""
        status,payload=self.db.execute('SELECT status,payload FROM zones WHERE h3=?',(h3,)).fetchone()
        return set() if status=='no_delivery' else {str(row['product_id']) for row in json.loads(payload)}

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None


def choose_samples(categories, counts, house, seed, pages=4):
    """Independent random pages are samples, never evidence of a full partition."""
    if type(pages) is not int or pages < 0:
        raise ValueError("pages must be a nonnegative integer")
    rng = random.Random(int.from_bytes(hashlib.sha256(
        json.dumps([seed, house], ensure_ascii=False).encode("utf-8")).digest(), "big"))
    groups = {"food": [], "household_chemicals": []}
    seen = set()
    for category in categories:
        if category.get("scope") not in groups or str(category["category_id"]) in seen:
            continue
        safe = core.public_url(category["url"])
        if not urlsplit(safe).path.startswith("/category/"):
            raise core.ParseError("Refresh sample needs a public category URL")
        seen.add(str(category["category_id"]))
        groups[category["scope"]].append(category)
    for group in groups.values():
        rng.shuffle(group)
    selected = []
    # Alternate scopes to give two from each when four pages and both are available.
    while len(selected) < pages and any(groups.values()):
        for scope in ("food", "household_chemicals"):
            if groups[scope] and len(selected) < pages:
                selected.append(groups[scope].pop())
    result = []
    for category in selected:
        maximum = max(1, int(counts.get(str(category["category_id"]), 1)))
        page = rng.randint(1, maximum)
        parts = urlsplit(core.public_url(category["url"]))
        url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                         urlencode({"miniapp": "supermarket", "page": page}), ""))
        result.append({"category": category, "page": page, "url": url})
    return result


class RefreshStore:
    def __init__(self, output, baseline_id, fingerprint, seconds):
        if not math.isfinite(float(seconds)) or float(seconds) <= 0:
            raise ValueError("seconds must be positive and finite")
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.output / "refresh.sqlite")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS houses(h3 TEXT PRIMARY KEY,status TEXT NOT NULL,
                summary TEXT NOT NULL,observations TEXT NOT NULL,pending TEXT NOT NULL);
        """)
        expected = {"baseline_id": str(baseline_id), "fingerprint": str(fingerprint),
                    "seconds": str(float(seconds))}
        old = dict(self.db.execute("SELECT key,value FROM meta"))
        if old and any(old.get(key) != value for key, value in expected.items()):
            self.db.close()
            raise core.ParseError("Refresh resume cannot change baseline, fingerprint or time budget")
        with self.db:
            for key, value in expected.items():
                self.db.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (key, value))
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('started_unix',?)", (str(time.time()),))
        self.started_unix = float(self.db.execute(
            "SELECT value FROM meta WHERE key='started_unix'").fetchone()[0])

    def done(self, h3):
        house = self.get_house(h3)
        return bool(house and house["status"] in ("sample_checked", "no_delivery"))

    def get_house(self, h3):
        record = self.db.execute("SELECT status,summary,observations,pending FROM houses WHERE h3=?", (h3,)).fetchone()
        return (dict(status=record[0], summary=json.loads(record[1]),
                     observations=json.loads(record[2]), pending=json.loads(record[3])) if record else None)

    def save_house(self, h3, summary, observations, pending, status):
        if status not in ("sample_checked", "no_delivery", "partial"):
            raise core.ParseError("Invalid sample refresh house status")
        normalized = {str(sku): _row(row, h3) for sku, row in observations.items()}
        if any(sku != row["product_id"] for sku, row in normalized.items()):
            raise core.ParseError("Refresh observation SKU key differs from its row")
        if not isinstance(pending, list) or not isinstance(summary, dict):
            raise core.ParseError("Refresh summary/pending have invalid types")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO houses VALUES(?,?,?,?,?)",
                (h3, status, json.dumps(summary, ensure_ascii=False),
                 json.dumps(normalized, ensure_ascii=False), json.dumps(pending, ensure_ascii=False)))

    def export(self, zones, baseline):
        report = dict(complete=False, sampling_plan_completed=True, houses_sample_checked=0,
                      houses_partial=0, houses_not_checked=0, observations_rows=0,
                      inferred_ordinary_prices=0,new_observed_products=0,houses_with_sample_errors=0,
                      unchecked_baseline_products=0, pending_changes=0, removed_confirmed=0,
                      baseline_id=baseline.identity, baseline_has_gaps=baseline.has_gaps,
                      started_unix=self.started_unix, elapsed_seconds=max(0, time.time()-self.started_unix))
        names = ("refresh_observations.csv", "coverage.jsonl", "unchecked_products.jsonl", "pending_changes.jsonl")
        streams = [(self.output / (name + ".tmp")).open("w", encoding="utf-8", newline="") for name in names]
        try:
            writer = csv.DictWriter(streams[0], fieldnames=core.COLUMNS); writer.writeheader()
            def emit(stream, data):
                stream.write(json.dumps(data, ensure_ascii=False) + "\n")
            for zone in zones:
                h3 = str(zone["h3_index"]); house = self.get_house(h3)
                base = baseline.skus(h3) if hasattr(baseline,'skus') else {row["product_id"] for row in baseline.house(h3)}
                status = house["status"] if house else "not_checked"
                staged = house["observations"] if house else {}
                confirmed = bool(house and (status == "sample_checked" or
                                             house["summary"].get("address_final_confirmed") is True))
                observations = staged if confirmed else {}
                bucket = ("houses_sample_checked" if status in ("sample_checked", "no_delivery")
                          else "houses_partial" if house else "houses_not_checked")
                report[bucket] += 1
                report["sampling_plan_completed"] &= status in ("sample_checked", "no_delivery")
                for row in observations.values():
                    writer.writerow(_row(row, h3)); report["observations_rows"] += 1
                    report['inferred_ordinary_prices']+=bool(json.loads(row['extra']).get('ordinary_price_inferred'))
                    report['new_observed_products']+=row['product_id'] not in base
                report['houses_with_sample_errors']+=bool(house and house['summary'].get('sample_errors'))
                unchecked = sorted(base - set(observations))
                report["unchecked_baseline_products"] += len(unchecked)
                for sku in unchecked:
                    emit(streams[2], {"h3_index": h3, "product_id": sku, "status": "unchecked", "removed_confirmed": False})
                pending = house["pending"] if house else []
                for change in pending:
                    emit(streams[3], dict(change, h3_index=h3)); report["pending_changes"] += 1
                coverage = dict(house["summary"] if house else {})
                coverage.update(h3_index=h3, status=status, sample_only=True, complete=False,
                                baseline_skus_count=len(base), observations_rows=len(observations),
                                staged_observations_rows=len(staged), address_final_confirmed=confirmed,
                                unchecked_baseline_skus=len(unchecked), pending_changes=len(pending))
                emit(streams[1], coverage)
        finally:
            for stream in streams:
                stream.close()
        for name in names:
            (self.output / (name + ".tmp")).replace(self.output / name)
        core.write_json(self.output / "refresh_report.json", report)
        return report

    def close(self):
        self.db.close()
