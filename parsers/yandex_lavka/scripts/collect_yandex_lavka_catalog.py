"""Collect a polite, bounded Yandex Lavka catalogue snapshot.

The collector reuses one guest session and one already documented public test
address.  It performs search only: no account, phone, cart, or checkout data is
created.  The first rate-limit or anti-bot response stops the run.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.catalog_sink import merge_products
from yandex_lavka.parser import (
    BASE_URL,
    LavkaClient,
    LavkaError,
    _clean_text,
    _money_to_minor,
    _parse_amount,
)


DEFAULT_OUTPUT_DIR = ROOT / "data" / "service_snapshots"
DEFAULT_ADDRESS = "Москва, Тверская улица, 1"

QUERY_GROUPS = [
    ("молоко", "молоко"),
    ("кефир", "кефир"),
    ("сметана", "сметана"),
    ("творог", "творог"),
    ("йогурт", "йогурт"),
    ("сыр", "сыр"),
    ("масло сливочное", "масло сливочное"),
    ("яйца", "яйца"),
    ("хлеб", "хлеб"),
    ("макароны", "макароны"),
    ("гречка", "крупы"),
    ("рис", "крупы"),
    ("мука", "мука"),
    ("сок", "сок"),
    ("вода", "вода"),
    ("чай", "чай"),
    ("кофе", "кофе"),
    ("детское питание", "детское питание"),
    ("овощи", "овощи"),
    ("фрукты", "фрукты"),
    ("мясо", "мясо"),
    ("курица", "птица"),
    ("колбаса", "колбаса"),
    ("рыба", "рыба"),
    ("консервы", "консервы"),
    ("соус", "соусы"),
    ("замороженные продукты", "замороженные продукты"),
]


def normalize_search(response: dict, *, currency: str, depot_type: str) -> list[dict]:
    products = response.get("cacheProducts")
    if not isinstance(products, list):
        raise LavkaError("UPSTREAM_CHANGED", "Lavka search response has no product list.")

    result: list[dict] = []
    for product in products:
        product_id = str(product.get("id") or "").strip()
        name = _clean_text(product.get("longTitle") or product.get("title") or "")
        if not product_id or not name:
            continue
        amount = _clean_text(product.get("amount") or "")
        quantity_value, quantity_unit = _parse_amount(amount)
        deep_link = str(product.get("deepLink") or "").strip()
        if deep_link.startswith("http"):
            url = deep_link
        elif deep_link:
            prefix = "/supermarket" if depot_type == "supermarket" else ""
            url = f"{BASE_URL}{prefix}/good/{quote(deep_link.strip('/'), safe='-_/')}"
        else:
            url = f"{BASE_URL}/search?text={quote(name)}"

        item = {
            "product_id": product_id,
            "name": name,
            "price": _money_to_minor(product.get("currentPrice", 0)),
            "currency": currency,
            "available": bool(product.get("available", False)),
            "url": url,
        }
        if quantity_value is not None:
            item["quantity_value"] = quantity_value
        if quantity_unit:
            item["quantity_unit"] = quantity_unit
        result.append(item)
    return result


def collect(*, output_dir: Path, address: str, limit: int, delay: float) -> dict:
    client = LavkaClient()
    location = client.resolve_location(address)
    totals = {"queries_completed": 0, "added": 0, "updated": 0, "stopped": None}

    for query, category_hint in QUERY_GROUPS:
        try:
            response = client.search_products(query, location, limit)
            products = normalize_search(
                response,
                currency=location.currency,
                depot_type=location.depot_type,
            )
            added, updated = merge_products(
                output_dir,
                "yandex_lavka",
                {
                    "service": "yandex_lavka",
                    "query": query,
                    "category_hint": category_hint,
                    "page_url": f"{BASE_URL}/search?text={quote(query)}",
                    "products": products,
                },
            )
            totals["queries_completed"] += 1
            totals["added"] += added
            totals["updated"] += updated
            print(
                json.dumps(
                    {"query": query, "received": len(products), "added": added, "updated": updated},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except LavkaError as error:
            if error.code in {"RATE_LIMITED", "ANTI_BOT_CHALLENGE"}:
                totals["stopped"] = error.to_dict()
                break
            raise
        time.sleep(delay)

    return totals


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--address", default=DEFAULT_ADDRESS)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--delay", type=float, default=1.5)
    args = parser.parse_args()
    totals = collect(
        output_dir=args.output_dir.resolve(),
        address=args.address,
        limit=max(1, min(args.limit, 50)),
        delay=max(args.delay, 1.0),
    )
    print(json.dumps(totals, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
