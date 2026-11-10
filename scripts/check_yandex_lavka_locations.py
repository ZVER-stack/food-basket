"""Safely inspect Lavka coverage for a small list of public addresses.

The script creates an independent guest session per address, resolves the
delivery depot and performs one search request. It never proceeds to checkout,
authentication or payment.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from yandex_lavka.parser import LavkaClient, LavkaError


def _first_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _delivery_summary(info: dict[str, Any]) -> dict[str, Any]:
    pricing = info.get("pricingConditions")
    if not isinstance(pricing, dict):
        pricing = {}
    conditions = info.get("deliveryConditions")
    if not isinstance(conditions, list):
        conditions = []
    delivery_condition = next(
        (
            condition
            for condition in conditions
            if isinstance(condition, dict) and condition.get("type") == "delivery"
        ),
        {},
    )
    template_vars = delivery_condition.get("templateVars")
    if not isinstance(template_vars, dict):
        template_vars = {}
    methods = info.get("deliveryMethods")
    if not isinstance(methods, dict):
        methods = {}
    return {
        "status": info.get("status"),
        "is_open": bool(info.get("isOpen")),
        "is_disabled": bool(info.get("isDisabled")),
        "time_text": info.get("deliveryTimeText"),
        "delivery_cost_rub": _first_value(pricing, "deliveryCost"),
        "full_delivery_cost_text": template_vars.get("fullDeliveryCost"),
        "minimum_order_rub": _first_value(pricing, "minimalCartPrice"),
        "high_demand": bool(info.get("isSurge") or info.get("showSurge")),
        "default_method": info.get("defaultDeliveryMethod"),
        "methods": methods,
    }


def inspect_address(address: str, query: str, limit: int) -> dict[str, Any]:
    client = LavkaClient()
    location = client.resolve_location(address)
    response = client.search_products(query, location, limit)
    products = response.get("cacheProducts")
    if not isinstance(products, list):
        raise LavkaError("UPSTREAM_CHANGED", "Lavka search response has no product list.")

    by_id = {
        str(product.get("id")): product
        for product in products
        if isinstance(product, dict) and product.get("id")
    }
    ordered_ids = [
        str(item.get("id"))
        for item in response.get("layoutItems", [])
        if isinstance(item, dict) and str(item.get("id")) in by_id
    ]
    ordered_ids.extend(product_id for product_id in by_id if product_id not in ordered_ids)

    top_products = []
    for product_id in ordered_ids[:limit]:
        product = by_id[product_id]
        top_products.append(
            {
                "product_id": product_id,
                "name": product.get("longTitle") or product.get("title") or "",
                "price_rub": product.get("currentPrice"),
                "old_price_rub": product.get("oldPrice"),
                "available": bool(product.get("available", False)),
                "amount": product.get("amount"),
            }
        )

    info = dict(location.service_info)
    return {
        "input_address": address,
        "resolved_address": {
            "city": location.city,
            "street": location.street,
            "house": location.house,
            "longitude": location.longitude,
            "latitude": location.latitude,
            "building_id": location.building_id,
        },
        "depot_id": location.depot_id,
        "depot_type": location.depot_type,
        "currency": location.currency,
        "delivery": _delivery_summary(info),
        "search_query": query,
        "products_returned": len(products),
        "top_products": top_products,
        "service_info_keys": sorted(info),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Проверить склады и один поисковый запрос для публичных адресов Лавки."
    )
    parser.add_argument("--address", action="append", required=True, help="Публичный адрес")
    parser.add_argument("--query", default="молоко 3,2%", help="Один безопасный поисковый запрос")
    parser.add_argument("--limit", type=int, default=3, choices=range(1, 11))
    parser.add_argument("--pause", type=float, default=3.0, help="Пауза между адресами, секунд")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    results: list[dict[str, Any]] = []
    for index, address in enumerate(args.address):
        if index:
            time.sleep(max(args.pause, 0))
        try:
            results.append(inspect_address(address, args.query, args.limit))
        except LavkaError as error:
            results.append({"input_address": address, "error": error.to_dict()})
            if error.code in {"RATE_LIMITED", "ANTI_BOT_CHALLENGE"}:
                break
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
