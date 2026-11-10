"""Collect Yandex Lavka catalogues for unique Moscow depots.

The collector uses the previously verified H3-to-depot map. Network work is
concurrent, while one shared limiter keeps request starts at least one second
apart. Product details are globally deduplicated by ``product_id``.
"""

from __future__ import annotations

import argparse
import random
import csv
import gzip
import hashlib
import json
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.collect_yandex_lavka_categories import (
    LAYOUT_SLUG,
    extract_categories,
    extract_products,
    normalize_product,
)
from scripts.lavka_city_store import CityCatalogStore
from yandex_lavka.parser import LavkaClient, LavkaError, ResolvedLocation


DEFAULT_MAPPING = ROOT / "data" / "yandex_lavka_hexagons" / "hexagon_depots.csv"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "yandex_lavka_city"
SAFETY_CODES = {"RATE_LIMITED", "ANTI_BOT_CHALLENGE", "AUTH_REQUIRED"}
RETRYABLE_CODES = {"SESSION_EXPIRED", "TIMEOUT", "UPSTREAM_UNAVAILABLE"}


class SafetyStop(RuntimeError):
    def __init__(self, error: LavkaError) -> None:
        super().__init__(str(error))
        self.error = error


class RequestRateLimiter:
    """Thread-safe global start-rate limiter."""

    def __init__(self, interval: float) -> None:
        self.interval = max(1.0, interval)
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_start:
                time.sleep(self._next_start - now)
                now = time.monotonic()
            jitter = random.uniform(2.0, 6.0)
            self._next_start = max(now, self._next_start) + self.interval + jitter


class RawArchive:
    """Append-only gzip JSONL archive; every append is a complete gzip member."""

    def __init__(self, root: Path, run_id: str) -> None:
        self.root = root / run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, stream: str, value: Mapping[str, Any]) -> None:
        path = self.root / f"{stream}.jsonl.gz"
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._lock:
            with gzip.open(path, "at", encoding="utf-8") as destination:
                destination.write(line)
                destination.write("\n")


def _mapping_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_mapping(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        rows = [dict(row) for row in csv.DictReader(source)]
    required = {"h3_index", "address", "address_lat", "address_lon", "storefront", "status"}
    if not rows:
        raise ValueError(f"Mapping is empty: {path}")
    missing = required - set(rows[0])
    if missing:
        raise ValueError(f"Mapping has no columns: {', '.join(sorted(missing))}")
    return rows


def parse_address_parts(address: str) -> tuple[str, str, str]:
    parts = [part.strip().strip('"') for part in address.split(",") if part.strip()]
    if len(parts) < 3:
        raise ValueError(f"Address must contain city, street and house: {address!r}")
    return parts[0], ", ".join(parts[1:-1]), parts[-1]


def unique_targets(
    rows: Iterable[Mapping[str, str]],
    storefronts: tuple[str, ...],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[Mapping[str, str]]] = {}
    for row in rows:
        storefront = str(row.get("storefront") or "")
        depot_id = str(row.get("depot_id") or "")
        if storefront not in storefronts or row.get("status") != "ok" or not depot_id:
            continue
        grouped.setdefault((storefront, depot_id), []).append(row)

    result: list[dict[str, Any]] = []
    for (storefront, depot_id), candidates in grouped.items():
        representative = sorted(
            candidates,
            key=lambda row: (
                str(row.get("address_in_zone", "")).casefold() != "true",
                row.get("h3_index", ""),
            ),
        )[0]
        city, street, house = parse_address_parts(str(representative["address"]))
        result.append({
            "storefront": storefront,
            "depot_id": depot_id,
            "h3_index": representative["h3_index"],
            "address": representative["address"],
            "city": city,
            "street": street,
            "house": house,
            "latitude": float(representative["address_lat"]),
            "longitude": float(representative["address_lon"]),
            "currency": representative.get("currency") or "RUB",
        })
    return sorted(
        result,
        key=lambda target: (
            0 if target["storefront"] == "regular" else 1,
            target["depot_id"],
        ),
    )


def target_location(target: Mapping[str, Any]) -> ResolvedLocation:
    return ResolvedLocation(
        city=str(target["city"]),
        street=str(target["street"]),
        house=str(target["house"]),
        longitude=float(target["longitude"]),
        latitude=float(target["latitude"]),
        depot_type=str(target["storefront"]),
        depot_id=str(target["depot_id"]),
        currency=str(target.get("currency") or "RUB"),
        currency_sign="₽",
    )


def target_payload(target: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "address": target["address"],
        "city": target["city"],
        "street": target["street"],
        "house": target["house"],
        "latitude": target["latitude"],
        "longitude": target["longitude"],
        "depot_type": target["storefront"],
    }


def _decimal(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _sticker_text(product: Mapping[str, Any]) -> str | None:
    values: list[str] = []
    for key in ("stickersV2", "stickers", "badges", "labels"):
        entries = product.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str) and entry.strip():
                values.append(entry.strip())
            elif isinstance(entry, Mapping):
                text = entry.get("text") or entry.get("title") or entry.get("label")
                if text:
                    values.append(str(text).strip())
    unique = list(dict.fromkeys(value for value in values if value))
    return "; ".join(unique) or None


def enrich_offer(product: dict[str, Any]) -> dict[str, Any]:
    raw = product.get("raw")
    if not isinstance(raw, Mapping):
        raw = {}
    product["promo"] = _sticker_text(raw)
    discount = raw.get("discountPricing")
    if isinstance(discount, Mapping):
        categories = discount.get("discountAnalyticsCategories")
        if isinstance(categories, list):
            for entry in categories:
                if isinstance(entry, Mapping) and entry.get("percentageValue") is not None:
                    product["discount_percent"] = _decimal(entry.get("percentageValue"))
                    break
    if product.get("discount_percent") is None:
        regular = product.get("old_price_minor")
        current = product.get("price_minor")
        if regular and current is not None and regular > current:
            product["discount_percent"] = round(100 * (regular - current) / regular, 2)
    if bool(raw.get("isLowStock")):
        product["stock_qty"] = _decimal(raw.get("maxAmount") or raw.get("quantityLimit"))
    product["offer_extra"] = {
        "quantity_limit": raw.get("quantityLimit"),
        "max_amount": raw.get("maxAmount"),
        "is_low_stock": raw.get("isLowStock"),
        "coming_soon": raw.get("comingSoon"),
        "pigeon_id": raw.get("pigeonId"),
    }
    return product


def _description_blocks(product: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    blocks = product.get("descriptionBlocks")
    return [block for block in blocks if isinstance(block, Mapping)] if isinstance(blocks, list) else []


def _block_value(block: Mapping[str, Any]) -> str:
    value = block.get("value")
    if value:
        return str(value).strip()
    items = block.get("items")
    if isinstance(items, list):
        texts = [str(item.get("text")).strip() for item in items if isinstance(item, Mapping) and item.get("text")]
        return " ".join(texts)
    return ""


def _split_shelf_storage(value: str) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    first, separator, rest = value.partition(",")
    return first.strip() or None, rest.strip() or None if separator else None


def _split_manufacturer_country(value: str) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    if "," not in value:
        return value.strip() or None, None
    manufacturer, country = value.rsplit(",", 1)
    return manufacturer.strip() or None, country.strip() or None


def extract_detail(response: Mapping[str, Any], location: ResolvedLocation) -> dict[str, Any]:
    raw_product = response.get("product")
    if not isinstance(raw_product, Mapping):
        raise LavkaError("UPSTREAM_CHANGED", "Lavka product response has no product object.")
    detail = normalize_product(raw_product, location=location, detailed=True)
    if detail is None:
        raise LavkaError("UPSTREAM_CHANGED", "Lavka returned an incomplete product detail.")

    composition = None
    shelf_life = None
    storage = None
    manufacturer = None
    country = None
    for block in _description_blocks(raw_product):
        attributes = block.get("attributes")
        attributes = set(attributes) if isinstance(attributes, list) else set()
        value = _block_value(block)
        if "ingredientsLoc" in attributes and value:
            composition = value
        if {"shelf_life_unit", "condition"} & attributes and value:
            shelf_life, storage = _split_shelf_storage(value)
        if {"manufacturer", "countryISO"} & attributes and value:
            manufacturer, country = _split_manufacturer_country(value)

    nutrients: dict[str, float | None] = {
        "kcal": None, "protein": None, "fat": None, "carbs": None,
    }
    options = raw_product.get("options")
    ingredients = options.get("ingredients") if isinstance(options, Mapping) else None
    traits = ingredients.get("pfcTraits") if isinstance(ingredients, Mapping) else None
    aliases = {"calories": "kcal", "protein": "protein", "fat": "fat", "carbohydrate": "carbs"}
    if isinstance(traits, list):
        for trait in traits:
            if not isinstance(trait, Mapping):
                continue
            key = aliases.get(str(trait.get("id") or ""))
            measures = trait.get("measures")
            if key and isinstance(measures, Mapping):
                nutrients[key] = _decimal(measures.get("per100g"))

    rating = _decimal(raw_product.get("rating") or raw_product.get("ratingValue"))
    reviews = raw_product.get("reviewsCount") or raw_product.get("ratingCount")
    labels = _sticker_text(raw_product)
    detail.update({
        "manufacturer": manufacturer,
        "country": country,
        **nutrients,
        "composition": composition,
        "shelf_life": shelf_life,
        "storage_conditions": storage,
        "rating": rating,
        "reviews_count": int(reviews) if str(reviews or "").isdigit() else None,
        "labels": labels,
    })
    return detail


def _new_client(rate_limiter: RequestRateLimiter, timeout: float) -> LavkaClient:
    return LavkaClient(timeout=timeout, before_request=rate_limiter.wait)


def collect_depot(
    *,
    store: CityCatalogStore,
    archive: RawArchive,
    run_id: str,
    target: Mapping[str, Any],
    rate_limiter: RequestRateLimiter,
    stop_event: threading.Event,
    timeout: float,
    verify_depot: bool,
    max_categories: int,
) -> dict[str, Any]:
    storefront = str(target["storefront"])
    depot_id = str(target["depot_id"])
    if stop_event.is_set():
        return {"storefront": storefront, "depot_id": depot_id, "status": "stopped"}
    store.set_depot_status(run_id, storefront, depot_id, "running")
    try:
        client = _new_client(rate_limiter, timeout)
        location = target_location(target)
        if verify_depot:
            resolved = client.resolve_location(target_payload(target))
            if str(resolved.depot_id) != depot_id:
                raise LavkaError(
                    "DEPOT_CHANGED",
                    f"Expected depot {depot_id}, received {resolved.depot_id}.",
                )
            location = resolved
            store.mark_depot_verified(storefront, depot_id)

        layout = client.get_category_layout(location, LAYOUT_SLUG)
        categories = extract_categories(layout)
        category_count = store.save_layout(
            run_id, storefront, depot_id, categories, max_categories=max_categories
        )
        archive.append("catalog", {
            "kind": "layout", "run_id": run_id, "storefront": storefront,
            "depot_id": depot_id, "response": layout,
        })

        completed = 0
        for task_row in store.pending_category_tasks(run_id, storefront, depot_id):
            if stop_event.is_set():
                break
            task = dict(task_row)
            response: Mapping[str, Any] | None = None
            error: LavkaError | None = None
            for attempt in range(2):
                try:
                    response = client.get_category_products(
                        location, str(task["category_id"]), str(task["group_id"]), LAYOUT_SLUG
                    )
                    error = None
                    break
                except LavkaError as caught:
                    error = caught
                    if caught.code in SAFETY_CODES:
                        stop_event.set()
                        raise SafetyStop(caught) from caught
                    if caught.code not in RETRYABLE_CODES or attempt == 1:
                        break
                    client = _new_client(rate_limiter, timeout)
            if error is not None or response is None:
                store.fail_category_task(
                    run_id, storefront, depot_id, str(task["category_id"]),
                    error.to_dict() if error else {"code": "EMPTY_RESPONSE"},
                )
                continue

            products = [enrich_offer(product) for product in extract_products(response, location)]
            added, updated = store.save_category_result(
                run_id, storefront, depot_id, task, products
            )
            archive.append("catalog", {
                "kind": "category", "run_id": run_id, "storefront": storefront,
                "depot_id": depot_id, "category_id": task["category_id"],
                "response": response,
            })
            completed += 1
            print(json.dumps({
                "stage": "catalog", "storefront": storefront, "depot_id": depot_id,
                "category": task.get("title"), "received": len(products),
                "new_products": added, "known_products": updated,
            }, ensure_ascii=False), flush=True)

        complete = store.finish_depot(run_id, storefront, depot_id)
        return {
            "storefront": storefront, "depot_id": depot_id,
            "status": "complete" if complete else "partial",
            "categories_planned": category_count, "categories_completed_now": completed,
        }
    except SafetyStop:
        store.set_depot_status(run_id, storefront, depot_id, "paused")
        raise
    except LavkaError as error:
        store.set_depot_status(run_id, storefront, depot_id, "error", error.to_dict())
        return {"storefront": storefront, "depot_id": depot_id, "status": "error", "error": error.to_dict()}
    except Exception as error:
        store.set_depot_status(run_id, storefront, depot_id, "error", {"message": str(error)})
        return {"storefront": storefront, "depot_id": depot_id, "status": "error", "error": {"message": str(error)}}


def collect_catalogs(
    *,
    store: CityCatalogStore,
    archive: RawArchive,
    run_id: str,
    workers: int,
    limiter: RequestRateLimiter,
    stop_event: threading.Event,
    timeout: float,
    verify_depots: bool,
    limit_depots: int,
    max_categories: int,
) -> list[dict[str, Any]]:
    targets = [dict(row) for row in store.pending_depots(run_id, limit=limit_depots)]
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="lavka-catalog") as pool:
        futures = [pool.submit(
            collect_depot,
            store=store, archive=archive, run_id=run_id, target=target,
            rate_limiter=limiter, stop_event=stop_event, timeout=timeout,
            verify_depot=verify_depots, max_categories=max_categories,
        ) for target in targets]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except SafetyStop:
                stop_event.set()
    return results


def collect_details(
    *,
    store: CityCatalogStore,
    archive: RawArchive,
    limiter: RequestRateLimiter,
    stop_event: threading.Event,
    workers: int,
    timeout: float,
    max_details: int,
) -> dict[str, int]:
    pending = [dict(row) for row in store.pending_details(limit=max_details)]
    jobs: queue.Queue[dict[str, Any]] = queue.Queue()
    for row in pending:
        jobs.put(row)
    counters = {"planned": len(pending), "complete": 0, "error": 0}
    counter_lock = threading.Lock()

    def worker() -> None:
        client: LavkaClient | None = None
        while True:
            try:
                item = jobs.get_nowait()
            except queue.Empty:
                return
            try:
                if stop_event.is_set():
                    continue
                if client is None:
                    client = _new_client(limiter, timeout)
                location = target_location(item)
                response: Mapping[str, Any] | None = None
                error: LavkaError | None = None
                for attempt in range(2):
                    try:
                        response = client.get_product_detail(location, str(item["product_id"]))
                        error = None
                        break
                    except LavkaError as caught:
                        error = caught
                        if caught.code in SAFETY_CODES:
                            stop_event.set()
                            break
                        if caught.code not in RETRYABLE_CODES or attempt == 1:
                            break
                        client = _new_client(limiter, timeout)
                if error is not None or response is None:
                    store.fail_detail(
                        str(item["product_id"]),
                        error.to_dict() if error else {"code": "EMPTY_RESPONSE"},
                    )
                    with counter_lock:
                        counters["error"] += 1
                    continue
                detail = extract_detail(response, location)
                store.save_detail(str(item["product_id"]), detail, response)
                archive.append("details", {
                    "kind": "product_detail", "product_id": item["product_id"],
                    "storefront": item["storefront"], "depot_id": item["depot_id"],
                    "response": response,
                })
                with counter_lock:
                    counters["complete"] += 1
                    completed = counters["complete"]
                if completed % 25 == 0 or completed == len(pending):
                    print(json.dumps({
                        "stage": "details", "completed": completed,
                        "planned": len(pending), "product_id": item["product_id"],
                    }, ensure_ascii=False), flush=True)
            except LavkaError as error:
                store.fail_detail(str(item["product_id"]), error.to_dict())
                with counter_lock:
                    counters["error"] += 1
            except Exception as error:
                store.fail_detail(str(item["product_id"]), {"message": str(error)})
                with counter_lock:
                    counters["error"] += 1
            finally:
                jobs.task_done()

    threads = [threading.Thread(target=worker, name=f"lavka-detail-{index + 1}") for index in range(max(1, workers))]
    for thread in threads:
        thread.start()
    jobs.join()
    for thread in threads:
        thread.join()
    return counters


def run_collection(args: argparse.Namespace) -> dict[str, Any]:
    mapping_path = args.mapping.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    mapping_rows = read_mapping(mapping_path)
    storefronts = tuple(args.storefront or ("regular", "supermarket"))
    targets = unique_targets(mapping_rows, storefronts)
    if args.limit_depots > 0:
        targets = targets[:args.limit_depots]
        selected = {(target["storefront"], target["depot_id"]) for target in targets}
        selected_h3 = {
            row["h3_index"] for row in mapping_rows
            if (row.get("storefront"), row.get("depot_id")) in selected
        }
        mapping_rows = [row for row in mapping_rows if row["h3_index"] in selected_h3]
    scope_key = ":".join([
        ",".join(storefronts), f"depots={args.limit_depots or 'all'}",
        f"categories={args.max_categories or 'all'}",
    ])
    limiter = RequestRateLimiter(args.request_interval)
    stop_event = threading.Event()
    database = output_dir / "city_catalog.sqlite3"

    with CityCatalogStore(database) as store:
        run_id, resumed = store.create_or_resume_run(
            _mapping_fingerprint(mapping_path), scope_key, resume=args.resume
        )
        store.register_mapping(run_id, mapping_rows, targets)
        archive = RawArchive(output_dir / "raw", run_id)
        catalog_results: list[dict[str, Any]] = []
        detail_results: dict[str, int] = {"planned": 0, "complete": 0, "error": 0}
        try:
            if args.stage in {"catalog", "all"}:
                catalog_results = collect_catalogs(
                    store=store, archive=archive, run_id=run_id,
                    workers=args.workers, limiter=limiter, stop_event=stop_event,
                    timeout=args.timeout, verify_depots=not args.skip_depot_verification,
                    limit_depots=0, max_categories=args.max_categories,
                )
                if stop_event.is_set():
                    store.set_run_status(run_id, "paused", {"code": "SAFETY_STOP"})
                else:
                    store.set_run_status(run_id, "catalog_complete")
            if args.stage in {"details", "all"} and not stop_event.is_set():
                detail_results = collect_details(
                    store=store, archive=archive, limiter=limiter,
                    stop_event=stop_event, workers=args.workers,
                    timeout=args.timeout, max_details=args.max_details,
                )
                if stop_event.is_set():
                    store.set_run_status(run_id, "paused", {"code": "SAFETY_STOP"})
                elif args.max_details > 0:
                    store.set_run_status(run_id, "paused")
                else:
                    current = store.summary(run_id)
                    remaining_details = sum(
                        count for status, count in current["product_details"].items()
                        if status != "complete"
                    )
                    incomplete_depots = sum(
                        count for status, count in current["depots"].items()
                        if status != "complete"
                    )
                    store.set_run_status(
                        run_id,
                        "complete" if remaining_details == 0 and incomplete_depots == 0 else "paused",
                    )
        except KeyboardInterrupt:
            stop_event.set()
            store.set_run_status(run_id, "paused", {"code": "INTERRUPTED"})
        except Exception as error:
            stop_event.set()
            store.set_run_status(run_id, "failed", {"message": str(error)})
            raise
        summary = store.summary(run_id)
        summary.update({
            "database": str(database), "raw_directory": str(archive.root),
            "resumed": resumed, "catalog_workers": args.workers,
            "request_interval_seconds": args.request_interval,
            "catalog_results": catalog_results,
            "detail_results": detail_results,
        })
        (output_dir / "latest_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Собрать каталог Яндекс Лавки по уникальным складам Москвы."
    )
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--storefront", action="append", choices=("regular", "supermarket"))
    parser.add_argument("--stage", choices=("catalog", "details", "all"), default="all")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--request-interval", type=float, default=1.05)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--skip-depot-verification", action="store_true")
    parser.add_argument("--limit-depots", type=int, default=0)
    parser.add_argument("--max-categories", type=int, default=0)
    parser.add_argument("--max-details", type=int, default=0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.workers = max(1, min(args.workers, 8))
    args.request_interval = max(1.0, args.request_interval)
    args.timeout = max(5.0, args.timeout)
    args.limit_depots = max(0, args.limit_depots)
    args.max_categories = max(0, args.max_categories)
    args.max_details = max(0, args.max_details)
    result = run_collection(args)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
