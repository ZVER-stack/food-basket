"""Collect the food catalogue of both Yandex Lavka storefronts by category.

The collector uses only a guest session and read-only catalogue endpoints. It
never authenticates, writes a cart, or proceeds to checkout. Category and
product-detail responses are preserved as JSONL while normalized records and
resume checkpoints are stored in SQLite.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lavka_catalog_store import CatalogStore
from yandex_lavka.parser import (
    BASE_URL,
    LavkaClient,
    LavkaError,
    ResolvedLocation,
    _clean_text,
    _money_to_minor,
    _parse_amount,
)


DEFAULT_OUTPUT_DIR = ROOT / "data" / "yandex_lavka_catalog"
DEFAULT_ADDRESS = "Москва, Рязанский проспект, 2к2"
DEFAULT_STOREFRONTS = ("regular", "supermarket")
LAYOUT_SLUG = "grocery"
SAFETY_ERROR_CODES = {"RATE_LIMITED", "ANTI_BOT_CHALLENGE", "AUTH_REQUIRED"}

# The grocery layout can expose links to adjacent Lavka hubs. Those sections
# are retained in SQLite with is_food=0 but are not downloaded in this run.
NON_FOOD_MARKERS = (
    "аптек", "лекарств", "витамин", "зоотовар", "для кош", "для собак",
    "корм для живот", "бытовая хим", "для дома", "товары для дома",
    "уборк", "стирк", "посуд", "гигиен", "космет", "красот",
    "уход за", "салфет", "бумажн", "цветы", "канцтовар", "носки",
    "одежд", "электрон",
)

FOOD_GROUP_TITLES = {
    "бакалея",
    "булочная и кондитерская",
    "вода и напитки",
    "готовая еда",
    "для детей",
    "заморозка",
    "молочный прилавок",
    "мясо, птица, рыба",
    "овощной прилавок",
    "сбалансированное питание",
    "сладкое и снеки",
}


class SafetyStop(RuntimeError):
    def __init__(self, error: LavkaError) -> None:
        super().__init__(str(error))
        self.error = error


def _append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        stream.write("\n")


def _is_food(group_title: str, category_title: str) -> bool:
    text = f"{group_title} {category_title}".casefold()
    return (
        group_title.casefold() in FOOD_GROUP_TITLES
        and not any(marker in text for marker in NON_FOOD_MARKERS)
    )


def extract_categories(layout: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize the group-to-category tree returned by ``v1/layout``."""

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    sections = layout.get("sections")
    if not isinstance(sections, list):
        raise LavkaError("UPSTREAM_CHANGED", "Lavka category layout has no sections list.")
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        category_group = section.get("categoryGroup")
        if not isinstance(category_group, Mapping):
            category_group = {}
        group_info = category_group.get("categoryGroupInfo")
        if not isinstance(group_info, Mapping):
            group_info = {}
        group_id = str(group_info.get("id") or "").strip()
        group_title = _clean_text(group_info.get("title") or "")
        categories = section.get("categories")
        if not isinstance(categories, list):
            continue
        for entry in categories:
            if not isinstance(entry, Mapping):
                continue
            info = entry.get("categoryInfo")
            if not isinstance(info, Mapping):
                continue
            category_id = str(info.get("id") or "").strip()
            if not category_id or not group_id or category_id in seen:
                continue
            seen.add(category_id)
            title = _clean_text(info.get("title") or "")
            result.append({
                "category_id": category_id,
                "group_id": group_id,
                "group_title": group_title,
                "title": title,
                "slug": str(info.get("deepLink") or "").strip() or None,
                "available": bool(info.get("available", True)),
                "is_food": _is_food(group_title, title),
                "raw": dict(info),
            })
    if not result:
        raise LavkaError("UPSTREAM_CHANGED", "Lavka category layout contained no categories.")
    return result


def _subcategory_map(raw: Mapping[str, Any]) -> dict[str, list[str]]:
    mapping: dict[str, set[str]] = {}

    def walk(nodes: Any, current: tuple[str, ...] = ()) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            value = node.get("value")
            if not isinstance(value, Mapping):
                value = {}
            value_type = str(value.get("type") or "")
            value_id = str(value.get("id") or "").strip()
            next_current = current
            if value_type == "subcategory" and value_id:
                next_current = (*current, value_id)
            elif value_type == "good" and value_id:
                mapping.setdefault(value_id, set()).update(current)
            walk(node.get("items"), next_current)

    walk(raw.get("categories"))
    return {product_id: sorted(values) for product_id, values in mapping.items()}


def _string_value(value: Any) -> str | None:
    if isinstance(value, str):
        return _clean_text(value) or None
    if isinstance(value, Mapping):
        for key in ("title", "name", "value", "label"):
            if value.get(key):
                return _clean_text(value[key]) or None
    return None


def _image_url(product: Mapping[str, Any]) -> str | None:
    for key in ("imageUrl", "image", "picture", "thumbnail"):
        value = product.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
        if isinstance(value, Mapping):
            for nested_key in ("url", "src", "original"):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested.startswith("http"):
                    return nested
    for key in ("pictures", "images", "media"):
        values = product.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str) and value.startswith("http"):
                return value
            if isinstance(value, Mapping):
                for nested_key in ("url", "src", "original"):
                    nested = value.get(nested_key)
                    if isinstance(nested, str) and nested.startswith("http"):
                        return nested
    return None


def normalize_product(
    product: Mapping[str, Any],
    *,
    location: ResolvedLocation,
    subcategory_ids: Iterable[str] = (),
    detailed: bool = False,
) -> dict[str, Any] | None:
    product_id = str(product.get("id") or product.get("product_id") or "").strip()
    name = _clean_text(
        product.get("longTitle") or product.get("title") or product.get("name") or ""
    )
    if not product_id or not name:
        return None
    amount = _clean_text(product.get("amount") or product.get("quantity") or "")
    quantity_value, quantity_unit = _parse_amount(amount)
    slug = str(product.get("deepLink") or product.get("slug") or "").strip()
    prefix = "/supermarket" if location.depot_type == "supermarket" else ""
    url = slug if slug.startswith("http") else (
        f"{BASE_URL}{prefix}/good/{quote(slug or product_id, safe='-_/')}"
    )
    price_value = product.get("currentPrice", product.get("price"))
    old_price_value = product.get("oldPrice")
    return {
        "product_id": product_id,
        "name": name,
        "brand": _string_value(product.get("brand")),
        "amount": amount or None,
        "quantity_value": quantity_value,
        "quantity_unit": quantity_unit,
        "price_minor": _money_to_minor(price_value) if price_value not in (None, "") else None,
        "old_price_minor": (
            _money_to_minor(old_price_value) if old_price_value not in (None, "") else None
        ),
        "currency": location.currency,
        "available": bool(product.get("available", product.get("inStock", True))),
        "slug": slug or None,
        "url": url,
        "image_url": _image_url(product),
        "description": _string_value(
            product.get("description") or product.get("shortDescription")
        ),
        "subcategory_ids": sorted(set(subcategory_ids)),
        "attributes": {
            "options": product.get("options"),
            "badges": product.get("badges"),
            "labels": product.get("labels"),
            "restrictions": product.get("restrictions"),
            "catalog_paths": product.get("catalogPaths"),
        },
        "raw": dict(product),
        "detail_status": "complete" if detailed else "pending",
    }


def extract_products(
    response: Mapping[str, Any], location: ResolvedLocation
) -> list[dict[str, Any]]:
    raw_products = response.get("products")
    if not isinstance(raw_products, list):
        raise LavkaError("UPSTREAM_CHANGED", "Lavka category response has no products list.")
    subcategories = _subcategory_map(response)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_products:
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("type") or "") in {"category", "category_group", "subcategory"}:
            continue
        product_id = str(raw.get("id") or raw.get("product_id") or "").strip()
        if not product_id or product_id in seen:
            continue
        normalized = normalize_product(
            raw,
            location=location,
            subcategory_ids=subcategories.get(product_id, []),
        )
        if normalized is not None:
            seen.add(product_id)
            result.append(normalized)
    return result


def _resolve_product_detail(response: Mapping[str, Any]) -> Mapping[str, Any]:
    product = response.get("product")
    if isinstance(product, Mapping):
        return product
    raise LavkaError("UPSTREAM_CHANGED", "Lavka product response has no product object.")


def _sleep(delay: float) -> None:
    time.sleep(max(delay, 1.0))


def collect_storefront(
    *,
    store: CatalogStore,
    run_id: str,
    raw_root: Path,
    address: str,
    depot_type: str,
    stage: str,
    delay: float,
    max_categories: int,
    max_details: int,
) -> dict[str, Any]:
    client = LavkaClient()
    location = client.resolve_location({"address": address, "depot_type": depot_type})
    store.save_storefront(run_id, location)
    raw_file = raw_root / run_id / f"{depot_type}.jsonl"
    completed_categories = 0
    category_products = 0
    details_completed = 0

    if stage in {"categories", "all"}:
        layout = client.get_category_layout(location, LAYOUT_SLUG)
        _append_jsonl(raw_file, {"kind": "layout", "depot_type": depot_type, "response": layout})
        categories = extract_categories(layout)
        store.save_categories(run_id, depot_type, categories)
        pending = store.pending_categories(run_id, depot_type)
        if max_categories > 0:
            pending = pending[:max_categories]
        for row in pending:
            category_id = str(row["category_id"])
            try:
                response = client.get_category_products(
                    location, category_id, str(row["group_id"]), LAYOUT_SLUG
                )
                products = extract_products(response, location)
                added, updated = store.save_products(
                    run_id, depot_type, str(location.depot_id), category_id, products
                )
                _append_jsonl(raw_file, {
                    "kind": "category",
                    "depot_type": depot_type,
                    "depot_id": location.depot_id,
                    "category_id": category_id,
                    "group_id": row["group_id"],
                    "response": response,
                })
                store.finish_category(run_id, depot_type, category_id, len(products))
                completed_categories += 1
                category_products += len(products)
                print(json.dumps({
                    "stage": "category", "storefront": depot_type,
                    "category": row["title"], "received": len(products),
                    "added": added, "updated": updated,
                }, ensure_ascii=False), flush=True)
            except LavkaError as error:
                store.fail_category(run_id, depot_type, category_id, error.to_dict())
                if error.code in SAFETY_ERROR_CODES:
                    raise SafetyStop(error) from error
                print(json.dumps({
                    "stage": "category", "storefront": depot_type,
                    "category_id": category_id, "error": error.to_dict(),
                }, ensure_ascii=False), flush=True)
            _sleep(delay)
        store.update_storefront_counts(run_id, depot_type)
        if max_categories == 0 and store.categories_complete(run_id, depot_type):
            marked_unavailable = store.mark_unseen_unavailable(
                run_id,
                depot_type,
                str(location.depot_id),
            )
            if marked_unavailable:
                print(json.dumps({
                    "stage": "availability", "storefront": depot_type,
                    "marked_unavailable": marked_unavailable,
                }, ensure_ascii=False), flush=True)

    if stage in {"details", "all"}:
        limit = max_details if max_details > 0 else None
        for row in store.pending_details(depot_type, str(location.depot_id), limit=limit):
            product_id = str(row["product_id"])
            try:
                response = client.get_product_detail(location, product_id)
                raw_product = _resolve_product_detail(response)
                product = normalize_product(raw_product, location=location, detailed=True)
                if product is None:
                    raise LavkaError(
                        "UPSTREAM_CHANGED",
                        f"Lavka returned an incomplete product detail for {product_id}.",
                    )
                store.save_detail(depot_type, str(location.depot_id), product)
                _append_jsonl(raw_file, {
                    "kind": "product_detail", "depot_type": depot_type,
                    "depot_id": location.depot_id, "product_id": product_id,
                    "response": response,
                })
                details_completed += 1
                print(json.dumps({
                    "stage": "detail", "storefront": depot_type,
                    "product_id": product_id, "name": product["name"],
                }, ensure_ascii=False), flush=True)
            except LavkaError as error:
                store.fail_detail(
                    depot_type, str(location.depot_id), product_id, error.to_dict()
                )
                if error.code in SAFETY_ERROR_CODES:
                    raise SafetyStop(error) from error
                print(json.dumps({
                    "stage": "detail", "storefront": depot_type,
                    "product_id": product_id, "error": error.to_dict(),
                }, ensure_ascii=False), flush=True)
            _sleep(delay)

    return {
        "storefront": depot_type,
        "depot_id": location.depot_id,
        "categories_completed_now": completed_categories,
        "product_cards_seen_now": category_products,
        "details_completed_now": details_completed,
    }


def collect(
    *,
    output_dir: Path,
    address: str,
    storefronts: Iterable[str],
    stage: str,
    delay: float,
    resume: bool,
    max_categories: int,
    max_details: int,
) -> dict[str, Any]:
    database = output_dir / "catalog.sqlite3"
    raw_root = output_dir / "raw"
    with CatalogStore(database) as store:
        run_id = store.latest_resumable_run(address, LAYOUT_SLUG) if resume else None
        if run_id is None:
            run_id = store.create_run(address, LAYOUT_SLUG)
        else:
            store.set_run_status(run_id, "running")
        current: list[dict[str, Any]] = []
        try:
            for index, depot_type in enumerate(storefronts):
                if index:
                    _sleep(delay)
                current.append(collect_storefront(
                    store=store, run_id=run_id, raw_root=raw_root,
                    address=address, depot_type=depot_type, stage=stage,
                    delay=delay, max_categories=max_categories,
                    max_details=max_details,
                ))
        except KeyboardInterrupt:
            store.set_run_status(run_id, "paused", {"code": "INTERRUPTED"})
        except SafetyStop as stop:
            store.set_run_status(run_id, "paused", stop.error.to_dict())
        except Exception as error:
            store.set_run_status(run_id, "failed", {"message": str(error)})
            raise
        else:
            limited = max_categories > 0 or max_details > 0 or stage != "all"
            store.set_run_status(run_id, "paused" if limited else "complete")
        return {
            "run_id": run_id,
            "database": str(database),
            "raw_directory": str(raw_root / run_id),
            "current_run": current,
            "summary": store.summary(run_id),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Собрать продуктовый каталог Яндекс Лавки по категориям."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--address", default=DEFAULT_ADDRESS)
    parser.add_argument(
        "--storefront", action="append", choices=DEFAULT_STOREFRONTS,
        help="Можно указать несколько раз. По умолчанию собираются обе витрины.",
    )
    parser.add_argument("--stage", choices=("categories", "details", "all"), default="all")
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-categories", type=int, default=0)
    parser.add_argument("--max-details", type=int, default=0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = collect(
        output_dir=args.output_dir.resolve(), address=args.address,
        storefronts=args.storefront or DEFAULT_STOREFRONTS, stage=args.stage,
        delay=max(args.delay, 1.0), resume=args.resume,
        max_categories=max(args.max_categories, 0),
        max_details=max(args.max_details, 0),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
