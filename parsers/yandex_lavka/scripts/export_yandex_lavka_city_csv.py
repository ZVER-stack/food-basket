"""Export one Moscow Yandex Lavka run to the required 37-column CSV."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "data" / "yandex_lavka_city" / "city_catalog.sqlite3"
MOSCOW_TZ = timezone(timedelta(hours=3), name="Europe/Moscow")

COLUMNS = [
    "service", "h3_index", "parsed_at", "store_id", "delivery_eta_min",
    "delivery_fee", "min_order", "product_id", "name", "brand",
    "manufacturer", "country", "category", "price", "price_regular",
    "price_card", "discount_percent", "promo", "amount", "unit_price",
    "unit_price_basis", "in_stock", "stock_qty", "rating", "reviews_count",
    "kcal", "protein", "fat", "carbs", "composition", "description",
    "shelf_life", "storage_conditions", "labels", "image_url", "url", "extra",
]


def _money(minor: Any) -> float | int | None:
    if minor is None:
        return None
    value = int(minor) / 100
    return int(value) if value.is_integer() else value


def _moscow_time(value: str | None) -> str:
    if not value:
        return datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW_TZ)
    return parsed.astimezone(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _eta_min(value: str | None) -> int | None:
    match = re.search(r"\d+", value or "")
    return int(match.group(0)) if match else None


def _open_output(path: Path) -> TextIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.casefold() == ".gz":
        return gzip.open(path, "wt", encoding="utf-8", newline="")
    return path.open("w", encoding="utf-8", newline="")


def latest_run(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise ValueError("The database contains no collection runs.")
    return str(row[0])


def export(database: Path, output: Path, run_id: str | None = None) -> dict[str, Any]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    run_id = run_id or latest_run(connection)
    sql = """
        SELECT
            z.h3_index, rz.storefront, rz.depot_id, rz.delivery_eta_text,
            rz.delivery_fee_rub, rz.min_order_rub,
            o.product_id, o.category_path, o.price_minor,
            o.regular_price_minor, o.price_card_minor, o.discount_percent,
            o.promo, o.amount AS offer_amount, o.unit_price,
            o.unit_price_basis, o.in_stock, o.stock_qty, o.currency,
            o.captured_at, o.extra_json,
            p.name, p.brand, p.manufacturer, p.country, p.amount,
            p.rating, p.reviews_count, p.kcal, p.protein, p.fat, p.carbs,
            p.composition, p.description, p.shelf_life,
            p.storage_conditions, p.labels, p.image_url, p.url
        FROM run_zone_depots rz
        JOIN zones z ON z.h3_index = rz.h3_index
        JOIN offers o
          ON o.run_id = rz.run_id
         AND o.storefront = rz.storefront
         AND o.depot_id = rz.depot_id
        JOIN products p
          ON p.service = 'lavka' AND p.product_id = o.product_id
        WHERE rz.run_id = ? AND rz.mapping_status = 'ok'
        ORDER BY z.h3_index, rz.storefront, o.product_id
    """
    zones_with_products: set[str] = set()
    row_count = 0
    with _open_output(output) as destination:
        writer = csv.DictWriter(destination, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for record in connection.execute(sql, (run_id,)):
            zones_with_products.add(str(record["h3_index"]))
            extra = json.loads(record["extra_json"] or "{}")
            extra.update({
                "storefront": record["storefront"],
                "currency": record["currency"],
            })
            row = {
                "service": "lavka",
                "h3_index": record["h3_index"],
                "parsed_at": _moscow_time(record["captured_at"]),
                "store_id": record["depot_id"],
                "delivery_eta_min": _eta_min(record["delivery_eta_text"]),
                "delivery_fee": record["delivery_fee_rub"],
                "min_order": record["min_order_rub"],
                "product_id": record["product_id"],
                "name": record["name"],
                "brand": record["brand"],
                "manufacturer": record["manufacturer"],
                "country": record["country"],
                "category": record["category_path"],
                "price": _money(record["price_minor"]),
                "price_regular": _money(record["regular_price_minor"]),
                "price_card": _money(record["price_card_minor"]),
                "discount_percent": record["discount_percent"],
                "promo": record["promo"],
                "amount": record["offer_amount"] or record["amount"],
                "unit_price": record["unit_price"],
                "unit_price_basis": record["unit_price_basis"],
                "in_stock": "true" if record["in_stock"] else "false",
                "stock_qty": record["stock_qty"],
                "rating": record["rating"],
                "reviews_count": record["reviews_count"],
                "kcal": record["kcal"],
                "protein": record["protein"],
                "fat": record["fat"],
                "carbs": record["carbs"],
                "composition": record["composition"],
                "description": record["description"],
                "shelf_life": record["shelf_life"],
                "storage_conditions": record["storage_conditions"],
                "labels": record["labels"],
                "image_url": record["image_url"],
                "url": record["url"],
                "extra": json.dumps(extra, ensure_ascii=False, separators=(",", ":")),
            }
            writer.writerow(row)
            row_count += 1

        all_zones = [str(row[0]) for row in connection.execute(
            "SELECT DISTINCT h3_index FROM run_zone_depots WHERE run_id = ? ORDER BY h3_index",
            (run_id,),
        )]
        for h3_index in all_zones:
            if h3_index in zones_with_products:
                continue
            writer.writerow({
                "service": "lavka", "h3_index": h3_index,
                "parsed_at": datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            })
            row_count += 1
    connection.close()
    return {"run_id": run_id, "output": str(output), "rows": row_count}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Экспортировать каталог Лавки в 37 колонок.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = export(args.database.resolve(), args.output.resolve(), args.run_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
