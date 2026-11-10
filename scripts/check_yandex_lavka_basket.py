"""Compare a five-product basket across two Lavka storefronts.

The script uses one guest session per storefront, never authenticates, and
does not proceed to checkout or payment.  For every product concept it first
tries to choose the same product id in both storefronts.  When this is not
possible, the report explicitly marks that two different candidates were used.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from yandex_lavka.parser import LavkaClient, LavkaError, ResolvedLocation, YandexLavkaParser


DEFAULT_QUERIES = (
    ("milk", "молоко 3,2% Село Зелёное 2 л"),
    ("bread", "хлеб дарницкий"),
    ("eggs", "яйца куриные С1 10 шт"),
    ("water", "вода негазированная 1,5 л"),
    ("cucumbers", "огурцы среднеплодные"),
)
DEPOT_TYPES = ("regular", "supermarket")


def _ordered_available_products(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    products = response.get("cacheProducts")
    if not isinstance(products, list):
        raise LavkaError("UPSTREAM_CHANGED", "Lavka search response has no product list.")
    by_id = {
        str(product.get("id")): product
        for product in products
        if isinstance(product, dict) and product.get("id") and product.get("available", False)
    }
    ordered_ids = [
        str(item.get("id"))
        for item in response.get("layoutItems", [])
        if isinstance(item, dict) and str(item.get("id")) in by_id
    ]
    ordered_ids.extend(product_id for product_id in by_id if product_id not in ordered_ids)
    return [by_id[product_id] for product_id in ordered_ids]


def _signature(product: Mapping[str, Any]) -> str:
    title = str(product.get("longTitle") or product.get("title") or "")
    amount = str(product.get("amount") or "")
    return re.sub(r"[^а-яёa-z0-9]+", " ", f"{title} {amount}".lower()).strip()


def _relevance(product: Mapping[str, Any], query: str) -> tuple[int, int]:
    """Prefer candidates containing more meaningful words from the query."""

    haystack = _signature(product)
    query_tokens = re.findall(r"[а-яёa-z0-9]+", query.lower())
    matched = sum(token in haystack.split() for token in query_tokens)
    return matched, -len(haystack)


def _product_summary(product: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "product_id": str(product.get("id")),
        "name": product.get("longTitle") or product.get("title") or "",
        "amount": product.get("amount"),
        "price_rub": product.get("currentPrice"),
        "old_price_rub": product.get("oldPrice"),
    }


def _location_input(location: ResolvedLocation) -> dict[str, Any]:
    return {
        "city": location.city,
        "street": location.street,
        "house": location.house,
        "longitude": location.longitude,
        "latitude": location.latitude,
        "building_id": location.building_id,
        "depot_type": location.depot_type,
    }


def inspect_basket(address: str, *, limit: int, pause: float) -> dict[str, Any]:
    clients = {depot_type: LavkaClient() for depot_type in DEPOT_TYPES}
    locations = {
        depot_type: client.resolve_location(
            {"address": address, "depot_type": depot_type}
        )
        for depot_type, client in clients.items()
    }

    candidates: dict[str, dict[str, list[dict[str, Any]]]] = {
        depot_type: {} for depot_type in DEPOT_TYPES
    }
    request_number = 0
    for concept, query in DEFAULT_QUERIES:
        for depot_type in DEPOT_TYPES:
            if request_number:
                time.sleep(max(pause, 0))
            response = clients[depot_type].search_products(
                query,
                locations[depot_type],
                limit,
            )
            candidates[depot_type][concept] = _ordered_available_products(response)
            request_number += 1

    selections: dict[str, list[dict[str, Any]]] = {
        depot_type: [] for depot_type in DEPOT_TYPES
    }
    matches: list[dict[str, Any]] = []
    for concept, query in DEFAULT_QUERIES:
        regular = candidates["regular"][concept]
        supermarket = candidates["supermarket"][concept]
        if not regular or not supermarket:
            missing = [
                depot_type
                for depot_type in DEPOT_TYPES
                if not candidates[depot_type][concept]
            ]
            matches.append(
                {
                    "concept": concept,
                    "query": query,
                    "match": "missing",
                    "missing_in": missing,
                }
            )
            continue

        supermarket_by_id = {str(product.get("id")): product for product in supermarket}
        selected_regular = max(regular, key=lambda product: _relevance(product, query))
        selected_supermarket = max(
            supermarket,
            key=lambda product: _relevance(product, query),
        )
        match = "different_candidates"

        common_candidates = [
            (product, supermarket_by_id[str(product.get("id"))])
            for product in regular
            if str(product.get("id")) in supermarket_by_id
        ]
        common = (
            max(common_candidates, key=lambda pair: _relevance(pair[0], query))
            if common_candidates
            else None
        )
        if common is not None:
            selected_regular, selected_supermarket = common
            match = "same_product_id"
        else:
            supermarket_by_signature = {_signature(product): product for product in supermarket}
            same_signature_candidates = [
                (product, supermarket_by_signature[_signature(product)])
                for product in regular
                if _signature(product) in supermarket_by_signature
            ]
            same_signature = (
                max(
                    same_signature_candidates,
                    key=lambda pair: _relevance(pair[0], query),
                )
                if same_signature_candidates
                else None
            )
            if same_signature is not None:
                selected_regular, selected_supermarket = same_signature
                match = "same_name_and_amount"

        for depot_type, product in (
            ("regular", selected_regular),
            ("supermarket", selected_supermarket),
        ):
            selections[depot_type].append(
                {
                    "concept": concept,
                    "query": query,
                    **_product_summary(product),
                }
            )
        matches.append(
            {
                "concept": concept,
                "query": query,
                "match": match,
                "regular_product_id": str(selected_regular.get("id")),
                "supermarket_product_id": str(selected_supermarket.get("id")),
            }
        )

    storefronts: dict[str, dict[str, Any]] = {}
    for depot_type in DEPOT_TYPES:
        time.sleep(max(pause, 0))
        parser = YandexLavkaParser(client_factory=lambda client=clients[depot_type]: client)
        cart = parser.calculate_cart_options(
            [
                {"product_id": product["product_id"], "quantity": 1}
                for product in selections[depot_type]
            ],
            _location_input(locations[depot_type]),
            depot_type=depot_type,
        )
        storefronts[depot_type] = {
            "resolved_depot_id": locations[depot_type].depot_id,
            "selected_products": selections[depot_type],
            "cart": cart,
        }

    return {
        "address": address,
        "matching": matches,
        "storefronts": storefronts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Сравнить контрольную корзину в двух витринах Яндекс Лавки."
    )
    parser.add_argument("--address", required=True, help="Публичный адрес доставки")
    parser.add_argument("--limit", type=int, default=20, choices=range(5, 51))
    parser.add_argument("--pause", type=float, default=2.0, help="Пауза между запросами")
    parser.add_argument("--output", type=Path, help="Необязательный путь для JSON-отчёта")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        result = inspect_basket(args.address, limit=args.limit, pause=args.pause)
    except LavkaError as error:
        result = {"address": args.address, "error": error.to_dict()}
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
