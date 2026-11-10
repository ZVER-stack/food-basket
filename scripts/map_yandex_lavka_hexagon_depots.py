"""Map Moscow H3 delivery points to Yandex Lavka depots.

This is the cheap discovery stage that must run before catalogue collection.
It performs one public ``service-info`` request per H3/storefront, groups zones
served by the same depot and never opens a cart or downloads product lists.
Progress is checkpointed to CSV after every request.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from yandex_lavka.parser import LavkaClient, LavkaError


try:
    MOSCOW_TZ = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:
    # Windows Python installations may not include the optional tzdata wheel.
    # Moscow has used UTC+3 without daylight-saving changes since 2014.
    MOSCOW_TZ = timezone(timedelta(hours=3), name="Europe/Moscow")
DEFAULT_OUTPUT = ROOT / "data" / "yandex_lavka_hexagons" / "hexagon_depots.csv"
DEFAULT_SUMMARY = ROOT / "data" / "yandex_lavka_hexagons" / "depot_summary.json"
DEFAULT_STOREFRONTS = ("regular", "supermarket")
SAFETY_STOP_CODES = {"RATE_LIMITED", "ANTI_BOT_CHALLENGE", "AUTH_REQUIRED"}
RETRYABLE_CODES = {"SESSION_EXPIRED", "TIMEOUT", "UPSTREAM_UNAVAILABLE"}

REQUIRED_INPUT_COLUMNS = {
    "h3_index",
    "address",
    "address_lat",
    "address_lon",
    "address_in_zone",
    "okrug",
    "district",
}

OUTPUT_COLUMNS = (
    "h3_index",
    "address",
    "address_lat",
    "address_lon",
    "address_in_zone",
    "okrug",
    "district",
    "storefront",
    "depot_id",
    "currency",
    "service_status",
    "is_open",
    "is_disabled",
    "delivery_eta_text",
    "delivery_fee_rub",
    "min_order_rub",
    "checked_at",
    "status",
    "error_code",
    "error_message",
)


def moscow_now() -> str:
    return datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S")


def parse_address_parts(address: str) -> tuple[str, str, str]:
    """Split ``city, street, house`` while preserving commas in street names."""

    parts = [part.strip().strip('"') for part in address.split(",") if part.strip()]
    if len(parts) < 3:
        raise ValueError(f"Address must contain city, street and house: {address!r}")
    city = parts[0]
    street = ", ".join(parts[1:-1])
    house = parts[-1]
    return city, street, house


def read_hexagons(path: Path, limit: int = 0) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        missing = REQUIRED_INPUT_COLUMNS - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"Missing columns in {path}: {', '.join(sorted(missing))}")
        rows = [dict(row) for row in reader]

    if len({row["h3_index"] for row in rows}) != len(rows):
        raise ValueError("hexagons.csv contains duplicate h3_index values")
    if limit > 0:
        return rows[:limit]
    return rows


def read_checkpoint(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    return {(row["h3_index"], row["storefront"]): row for row in rows}


def write_checkpoint(
    path: Path,
    rows_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    zone_order: Mapping[str, int],
    storefront_order: Mapping[str, int],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    ordered = sorted(
        rows_by_key.values(),
        key=lambda row: (
            zone_order.get(str(row["h3_index"]), len(zone_order)),
            storefront_order.get(str(row["storefront"]), len(storefront_order)),
        ),
    )
    with temporary.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ordered)
    os.replace(temporary, path)


def _first_value(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return ""


def _delivery_fields(info: Mapping[str, Any]) -> dict[str, Any]:
    pricing = info.get("pricingConditions")
    if not isinstance(pricing, Mapping):
        pricing = {}
    return {
        "service_status": info.get("status", ""),
        "is_open": str(bool(info.get("isOpen"))).lower(),
        "is_disabled": str(bool(info.get("isDisabled"))).lower(),
        "delivery_eta_text": info.get("deliveryTimeText", ""),
        "delivery_fee_rub": _first_value(pricing, "deliveryCost"),
        "min_order_rub": _first_value(pricing, "minimalCartPrice"),
    }


def location_payload(zone: Mapping[str, str], storefront: str) -> dict[str, Any]:
    city, street, house = parse_address_parts(zone["address"])
    return {
        "address": zone["address"],
        "city": city,
        "street": street,
        "house": house,
        "latitude": float(zone["address_lat"]),
        "longitude": float(zone["address_lon"]),
        "depot_type": storefront,
    }


def base_result(zone: Mapping[str, str], storefront: str) -> dict[str, Any]:
    return {
        "h3_index": zone["h3_index"],
        "address": zone["address"],
        "address_lat": zone["address_lat"],
        "address_lon": zone["address_lon"],
        "address_in_zone": zone["address_in_zone"],
        "okrug": zone["okrug"],
        "district": zone["district"],
        "storefront": storefront,
        "depot_id": "",
        "currency": "",
        "service_status": "",
        "is_open": "",
        "is_disabled": "",
        "delivery_eta_text": "",
        "delivery_fee_rub": "",
        "min_order_rub": "",
        "checked_at": moscow_now(),
        "status": "error",
        "error_code": "",
        "error_message": "",
    }


def summarize(
    zones: Iterable[Mapping[str, str]],
    rows_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    storefronts: Iterable[str],
) -> dict[str, Any]:
    zone_list = list(zones)
    storefront_list = list(storefronts)
    rows = list(rows_by_key.values())
    successful = [row for row in rows if row.get("status") == "ok" and row.get("depot_id")]
    status_counts = Counter(str(row.get("status") or "unknown") for row in rows)
    unique_by_storefront = {
        storefront: len({
            str(row["depot_id"])
            for row in successful
            if row.get("storefront") == storefront
        })
        for storefront in storefront_list
    }
    depot_zone_counts = Counter(
        (str(row["storefront"]), str(row["depot_id"])) for row in successful
    )

    depots_by_zone: dict[str, dict[str, str]] = defaultdict(dict)
    for row in successful:
        depots_by_zone[str(row["h3_index"])][str(row["storefront"])] = str(row["depot_id"])
    complete_profiles = {
        tuple(profile.get(storefront, "") for storefront in storefront_list)
        for profile in depots_by_zone.values()
        if all(profile.get(storefront) for storefront in storefront_list)
    }
    zone_coverage = Counter()
    for zone in zone_list:
        available = depots_by_zone.get(str(zone["h3_index"]), {})
        present = tuple(storefront for storefront in storefront_list if available.get(storefront))
        if len(present) == len(storefront_list):
            zone_coverage["all_storefronts"] += 1
        elif not present:
            zone_coverage["no_storefronts"] += 1
        else:
            zone_coverage[f"only_{'_'.join(present)}"] += 1

    requested_pairs = len(zone_list) * len(storefront_list)
    unique_depot_storefronts = len(depot_zone_counts)
    reduction = 0.0
    if requested_pairs:
        reduction = 100 * (1 - unique_depot_storefronts / requested_pairs)

    return {
        "zones_in_input": len(zone_list),
        "storefronts": storefront_list,
        "requested_zone_storefront_pairs": requested_pairs,
        "supported_zone_storefront_pairs": len(successful),
        "checked_rows": len(rows),
        "status_counts": dict(sorted(status_counts.items())),
        "zone_coverage": dict(sorted(zone_coverage.items())),
        "unique_depots_by_storefront": unique_by_storefront,
        "unique_depot_storefront_pairs": unique_depot_storefronts,
        "unique_complete_depot_profiles": len(complete_profiles),
        "catalog_runs_after_deduplication": unique_depot_storefronts,
        "catalog_run_reduction_percent": round(reduction, 2),
        "catalog_run_reduction_from_supported_percent": round(
            100 * (1 - unique_depot_storefronts / len(successful)), 2
        ) if successful else 0.0,
        "largest_depot_groups": [
            {"storefront": key[0], "depot_id": key[1], "zones": count}
            for key, count in depot_zone_counts.most_common(20)
        ],
        "generated_at": moscow_now(),
    }


def save_summary(path: Path, summary: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def map_depots(
    *,
    input_path: Path,
    output_path: Path,
    summary_path: Path,
    storefronts: tuple[str, ...],
    pause: float,
    limit: int,
    timeout: float,
    retry_errors: bool,
) -> dict[str, Any]:
    zones = read_hexagons(input_path, limit=limit)
    checkpoint = read_checkpoint(output_path)
    zone_order = {zone["h3_index"]: index for index, zone in enumerate(zones)}
    storefront_order = {value: index for index, value in enumerate(storefronts)}
    minimum_pause = max(1.0, pause)

    client = LavkaClient(timeout=timeout)
    last_request_at = time.monotonic()
    total = len(zones) * len(storefronts)
    completed = 0

    for zone in zones:
        for storefront in storefronts:
            completed += 1
            key = (zone["h3_index"], storefront)
            existing = checkpoint.get(key)
            if existing and (not retry_errors or existing.get("status") == "ok"):
                print(f"[{completed}/{total}] {zone['h3_index']} {storefront}: cached", flush=True)
                continue

            elapsed = time.monotonic() - last_request_at
            if elapsed < minimum_pause:
                time.sleep(minimum_pause - elapsed)

            result = base_result(zone, storefront)
            for attempt in range(2):
                try:
                    resolved = client.resolve_location(location_payload(zone, storefront))
                    last_request_at = time.monotonic()
                    result.update({
                        "depot_id": resolved.depot_id or "",
                        "currency": resolved.currency,
                        **_delivery_fields(resolved.service_info),
                        "status": "ok",
                    })
                    break
                except LavkaError as error:
                    last_request_at = time.monotonic()
                    result.update({
                        "status": "unsupported" if error.code == "LOCATION_UNSUPPORTED" else "error",
                        "error_code": error.code,
                        "error_message": str(error),
                    })
                    if error.code in SAFETY_STOP_CODES:
                        checkpoint[key] = result
                        write_checkpoint(output_path, checkpoint, zone_order, storefront_order)
                        summary = summarize(zones, checkpoint, storefronts)
                        summary["stopped_by"] = error.to_dict()
                        save_summary(summary_path, summary)
                        raise
                    if error.code not in RETRYABLE_CODES or attempt == 1:
                        break
                    time.sleep(minimum_pause)
                    client = LavkaClient(timeout=timeout)
                    last_request_at = time.monotonic()

            checkpoint[key] = result
            write_checkpoint(output_path, checkpoint, zone_order, storefront_order)
            print(
                f"[{completed}/{total}] {zone['h3_index']} {storefront}: "
                f"{result['status']} {result['depot_id'] or result['error_code']}",
                flush=True,
            )

    summary = summarize(zones, checkpoint, storefronts)
    save_summary(summary_path, summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Сопоставить H3-зоны Москвы со складами двух витрин Яндекс Лавки."
    )
    parser.add_argument("--input", type=Path, required=True, help="Путь к hexagons.csv")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument(
        "--storefront", action="append", choices=DEFAULT_STOREFRONTS,
        help="Можно указать несколько раз; по умолчанию обе витрины.",
    )
    parser.add_argument("--pause", type=float, default=1.0, help="Пауза между запросами, минимум 1 секунда")
    parser.add_argument("--limit", type=int, default=0, help="Проверить только первые N гексагонов")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retry-errors", action="store_true", help="Повторить строки со статусом error")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    summary = map_depots(
        input_path=args.input.resolve(),
        output_path=args.output.resolve(),
        summary_path=args.summary.resolve(),
        storefronts=tuple(args.storefront or DEFAULT_STOREFRONTS),
        pause=max(1.0, args.pause),
        limit=max(0, args.limit),
        timeout=max(1.0, args.timeout),
        retry_errors=args.retry_errors,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
