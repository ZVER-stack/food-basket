"""Thread-safe SQLite storage for the Moscow-wide Yandex Lavka collector."""

from __future__ import annotations

import csv
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SERVICE = "lavka"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return f"lavka-city-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _value(value: Any) -> Any:
    return None if value in (None, "") else value


class CityCatalogStore:
    """One connection guarded by a lock; network workers never write concurrently."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def __enter__(self) -> "CityCatalogStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        with self._lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    mapping_fingerprint TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    last_error_json TEXT
                );

                CREATE TABLE IF NOT EXISTS zones (
                    h3_index TEXT PRIMARY KEY,
                    address TEXT NOT NULL,
                    address_lat REAL,
                    address_lon REAL,
                    address_in_zone INTEGER,
                    okrug TEXT,
                    district TEXT
                );

                CREATE TABLE IF NOT EXISTS run_zone_depots (
                    run_id TEXT NOT NULL,
                    h3_index TEXT NOT NULL,
                    storefront TEXT NOT NULL,
                    depot_id TEXT,
                    currency TEXT,
                    service_status TEXT,
                    delivery_eta_text TEXT,
                    delivery_fee_rub REAL,
                    min_order_rub REAL,
                    mapping_checked_at TEXT,
                    mapping_status TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    PRIMARY KEY (run_id, h3_index, storefront),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id),
                    FOREIGN KEY (h3_index) REFERENCES zones(h3_index)
                );

                CREATE TABLE IF NOT EXISTS depots (
                    storefront TEXT NOT NULL,
                    depot_id TEXT NOT NULL,
                    representative_h3 TEXT NOT NULL,
                    address TEXT NOT NULL,
                    city TEXT NOT NULL,
                    street TEXT NOT NULL,
                    house TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'RUB',
                    last_verified_at TEXT,
                    PRIMARY KEY (storefront, depot_id)
                );

                CREATE TABLE IF NOT EXISTS depot_runs (
                    run_id TEXT NOT NULL,
                    storefront TEXT NOT NULL,
                    depot_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    started_at TEXT,
                    completed_at TEXT,
                    category_count INTEGER NOT NULL DEFAULT 0,
                    product_count INTEGER NOT NULL DEFAULT 0,
                    error_json TEXT,
                    PRIMARY KEY (run_id, storefront, depot_id),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS categories (
                    storefront TEXT NOT NULL,
                    category_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    group_title TEXT,
                    title TEXT,
                    slug TEXT,
                    is_food INTEGER NOT NULL,
                    raw_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (storefront, category_id)
                );

                CREATE TABLE IF NOT EXISTS category_tasks (
                    run_id TEXT NOT NULL,
                    storefront TEXT NOT NULL,
                    depot_id TEXT NOT NULL,
                    category_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    product_count INTEGER NOT NULL DEFAULT 0,
                    captured_at TEXT,
                    error_json TEXT,
                    PRIMARY KEY (run_id, storefront, depot_id, category_id),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS products (
                    service TEXT NOT NULL DEFAULT 'lavka',
                    product_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    brand TEXT,
                    manufacturer TEXT,
                    country TEXT,
                    amount TEXT,
                    quantity_value REAL,
                    quantity_unit TEXT,
                    kcal REAL,
                    protein REAL,
                    fat REAL,
                    carbs REAL,
                    composition TEXT,
                    description TEXT,
                    shelf_life TEXT,
                    storage_conditions TEXT,
                    rating REAL,
                    reviews_count INTEGER,
                    labels TEXT,
                    image_url TEXT,
                    url TEXT,
                    preferred_storefront TEXT NOT NULL,
                    preferred_depot_id TEXT NOT NULL,
                    detail_status TEXT NOT NULL DEFAULT 'pending',
                    detail_attempts INTEGER NOT NULL DEFAULT 0,
                    detail_error_json TEXT,
                    raw_detail_json TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    detailed_at TEXT,
                    PRIMARY KEY (service, product_id)
                );

                CREATE TABLE IF NOT EXISTS offers (
                    run_id TEXT NOT NULL,
                    storefront TEXT NOT NULL,
                    depot_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    category_path TEXT,
                    price_minor INTEGER,
                    regular_price_minor INTEGER,
                    price_card_minor INTEGER,
                    discount_percent REAL,
                    promo TEXT,
                    amount TEXT,
                    unit_price REAL,
                    unit_price_basis TEXT,
                    in_stock INTEGER,
                    stock_qty REAL,
                    currency TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    extra_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, storefront, depot_id, product_id),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS offer_categories (
                    run_id TEXT NOT NULL,
                    storefront TEXT NOT NULL,
                    depot_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    category_id TEXT NOT NULL,
                    subcategory_ids_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, storefront, depot_id, product_id, category_id)
                );

                CREATE INDEX IF NOT EXISTS idx_runs_resume
                    ON runs(mapping_fingerprint, scope_key, status, started_at);
                CREATE INDEX IF NOT EXISTS idx_depot_runs_status
                    ON depot_runs(run_id, status);
                CREATE INDEX IF NOT EXISTS idx_category_tasks_status
                    ON category_tasks(run_id, storefront, depot_id, status);
                CREATE INDEX IF NOT EXISTS idx_products_detail_status
                    ON products(detail_status, product_id);
                CREATE INDEX IF NOT EXISTS idx_offers_product
                    ON offers(product_id, run_id);
                """
            )
            self.connection.commit()

    def create_or_resume_run(
        self,
        mapping_fingerprint: str,
        scope_key: str,
        *,
        resume: bool,
    ) -> tuple[str, bool]:
        with self._lock:
            if resume:
                row = self.connection.execute(
                    """
                    SELECT run_id FROM runs
                    WHERE mapping_fingerprint = ? AND scope_key = ?
                      AND status != 'complete'
                    ORDER BY started_at DESC LIMIT 1
                    """,
                    (mapping_fingerprint, scope_key),
                ).fetchone()
                if row:
                    run_id = str(row["run_id"])
                    self.set_run_status(run_id, "running")
                    return run_id, True
            run_id = new_run_id()
            now = utc_now()
            self.connection.execute(
                """
                INSERT INTO runs(
                    run_id, mapping_fingerprint, scope_key, status, started_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, ?)
                """,
                (run_id, mapping_fingerprint, scope_key, now, now),
            )
            self.connection.commit()
            return run_id, False

    def set_run_status(self, run_id: str, status: str, error: Any = None) -> None:
        with self._lock:
            now = utc_now()
            completed = now if status == "complete" else None
            self.connection.execute(
                """
                UPDATE runs
                SET status = ?, updated_at = ?, completed_at = ?, last_error_json = ?
                WHERE run_id = ?
                """,
                (status, now, completed, _json(error) if error is not None else None, run_id),
            )
            self.connection.commit()

    def register_mapping(
        self,
        run_id: str,
        rows: Iterable[Mapping[str, Any]],
        targets: Iterable[Mapping[str, Any]],
    ) -> None:
        with self._lock:
            for row in rows:
                self.connection.execute(
                    """
                    INSERT INTO zones(
                        h3_index, address, address_lat, address_lon,
                        address_in_zone, okrug, district
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(h3_index) DO UPDATE SET
                        address = excluded.address,
                        address_lat = excluded.address_lat,
                        address_lon = excluded.address_lon,
                        address_in_zone = excluded.address_in_zone,
                        okrug = excluded.okrug,
                        district = excluded.district
                    """,
                    (
                        row["h3_index"], row["address"], _value(row.get("address_lat")),
                        _value(row.get("address_lon")),
                        int(str(row.get("address_in_zone", "")).casefold() == "true"),
                        row.get("okrug"), row.get("district"),
                    ),
                )
                self.connection.execute(
                    """
                    INSERT INTO run_zone_depots(
                        run_id, h3_index, storefront, depot_id, currency,
                        service_status, delivery_eta_text, delivery_fee_rub,
                        min_order_rub, mapping_checked_at, mapping_status,
                        error_code, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, h3_index, storefront) DO UPDATE SET
                        depot_id = excluded.depot_id,
                        currency = excluded.currency,
                        service_status = excluded.service_status,
                        delivery_eta_text = excluded.delivery_eta_text,
                        delivery_fee_rub = excluded.delivery_fee_rub,
                        min_order_rub = excluded.min_order_rub,
                        mapping_checked_at = excluded.mapping_checked_at,
                        mapping_status = excluded.mapping_status,
                        error_code = excluded.error_code,
                        error_message = excluded.error_message
                    """,
                    (
                        run_id, row["h3_index"], row["storefront"],
                        _value(row.get("depot_id")), _value(row.get("currency")),
                        _value(row.get("service_status")),
                        _value(row.get("delivery_eta_text")),
                        _value(row.get("delivery_fee_rub")),
                        _value(row.get("min_order_rub")), _value(row.get("checked_at")),
                        row.get("status") or "error", _value(row.get("error_code")),
                        _value(row.get("error_message")),
                    ),
                )
            for target in targets:
                self.connection.execute(
                    """
                    INSERT INTO depots(
                        storefront, depot_id, representative_h3, address, city,
                        street, house, latitude, longitude, currency
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(storefront, depot_id) DO UPDATE SET
                        representative_h3 = excluded.representative_h3,
                        address = excluded.address,
                        city = excluded.city,
                        street = excluded.street,
                        house = excluded.house,
                        latitude = excluded.latitude,
                        longitude = excluded.longitude,
                        currency = excluded.currency
                    """,
                    (
                        target["storefront"], target["depot_id"], target["h3_index"],
                        target["address"], target["city"], target["street"],
                        target["house"], target["latitude"], target["longitude"],
                        target.get("currency") or "RUB",
                    ),
                )
                self.connection.execute(
                    """
                    INSERT INTO depot_runs(run_id, storefront, depot_id)
                    VALUES (?, ?, ?)
                    ON CONFLICT(run_id, storefront, depot_id) DO NOTHING
                    """,
                    (run_id, target["storefront"], target["depot_id"]),
                )
            self.connection.commit()

    def pending_depots(self, run_id: str, limit: int = 0) -> list[sqlite3.Row]:
        with self._lock:
            sql = """
                SELECT d.*, dr.status
                FROM depot_runs dr
                JOIN depots d USING(storefront, depot_id)
                WHERE dr.run_id = ? AND dr.status != 'complete'
                ORDER BY CASE d.storefront WHEN 'regular' THEN 0 ELSE 1 END, d.depot_id
            """
            params: list[Any] = [run_id]
            if limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            return list(self.connection.execute(sql, params))

    def set_depot_status(
        self,
        run_id: str,
        storefront: str,
        depot_id: str,
        status: str,
        error: Any = None,
    ) -> None:
        with self._lock:
            now = utc_now()
            self.connection.execute(
                """
                UPDATE depot_runs SET
                    status = ?,
                    started_at = COALESCE(started_at, ?),
                    completed_at = CASE WHEN ? = 'complete' THEN ? ELSE NULL END,
                    error_json = ?
                WHERE run_id = ? AND storefront = ? AND depot_id = ?
                """,
                (
                    status, now, status, now,
                    _json(error) if error is not None else None,
                    run_id, storefront, depot_id,
                ),
            )
            self.connection.commit()

    def mark_depot_verified(self, storefront: str, depot_id: str) -> None:
        with self._lock:
            self.connection.execute(
                "UPDATE depots SET last_verified_at = ? WHERE storefront = ? AND depot_id = ?",
                (utc_now(), storefront, depot_id),
            )
            self.connection.commit()

    def save_layout(
        self,
        run_id: str,
        storefront: str,
        depot_id: str,
        categories: Iterable[Mapping[str, Any]],
        *,
        max_categories: int = 0,
    ) -> int:
        food = [category for category in categories if category.get("is_food")]
        if max_categories > 0:
            food = food[:max_categories]
        now = utc_now()
        with self._lock:
            for category in food:
                self.connection.execute(
                    """
                    INSERT INTO categories(
                        storefront, category_id, group_id, group_title, title,
                        slug, is_food, raw_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(storefront, category_id) DO UPDATE SET
                        group_id = excluded.group_id,
                        group_title = excluded.group_title,
                        title = excluded.title,
                        slug = excluded.slug,
                        is_food = excluded.is_food,
                        raw_json = excluded.raw_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        storefront, category["category_id"], category["group_id"],
                        category.get("group_title"), category.get("title"),
                        category.get("slug"), _json(category.get("raw", {})), now,
                    ),
                )
                self.connection.execute(
                    """
                    INSERT INTO category_tasks(
                        run_id, storefront, depot_id, category_id
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(run_id, storefront, depot_id, category_id) DO NOTHING
                    """,
                    (run_id, storefront, depot_id, category["category_id"]),
                )
            self.connection.execute(
                """
                UPDATE depot_runs SET category_count = ?
                WHERE run_id = ? AND storefront = ? AND depot_id = ?
                """,
                (len(food), run_id, storefront, depot_id),
            )
            self.connection.commit()
        return len(food)

    def pending_category_tasks(
        self, run_id: str, storefront: str, depot_id: str
    ) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.connection.execute(
                    """
                    SELECT t.*, c.group_id, c.group_title, c.title
                    FROM category_tasks t
                    JOIN categories c USING(storefront, category_id)
                    WHERE t.run_id = ? AND t.storefront = ? AND t.depot_id = ?
                      AND t.status != 'complete'
                    ORDER BY c.group_title, c.title, t.category_id
                    """,
                    (run_id, storefront, depot_id),
                )
            )

    def save_category_result(
        self,
        run_id: str,
        storefront: str,
        depot_id: str,
        task: Mapping[str, Any],
        products: Iterable[Mapping[str, Any]],
    ) -> tuple[int, int]:
        now = utc_now()
        added = 0
        updated = 0
        category_path = " > ".join(
            part for part in (task.get("group_title"), task.get("title")) if part
        )
        with self._lock:
            for product in products:
                product_id = str(product["product_id"])
                exists = self.connection.execute(
                    "SELECT 1 FROM products WHERE service = ? AND product_id = ?",
                    (SERVICE, product_id),
                ).fetchone()
                if exists:
                    updated += 1
                else:
                    added += 1
                self.connection.execute(
                    """
                    INSERT INTO products(
                        service, product_id, name, brand, amount, quantity_value,
                        quantity_unit, description, image_url, url,
                        preferred_storefront, preferred_depot_id,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(service, product_id) DO UPDATE SET
                        name = excluded.name,
                        brand = COALESCE(products.brand, excluded.brand),
                        amount = COALESCE(excluded.amount, products.amount),
                        quantity_value = COALESCE(excluded.quantity_value, products.quantity_value),
                        quantity_unit = COALESCE(excluded.quantity_unit, products.quantity_unit),
                        description = COALESCE(products.description, excluded.description),
                        image_url = COALESCE(products.image_url, excluded.image_url),
                        url = COALESCE(products.url, excluded.url),
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        SERVICE, product_id, product["name"], product.get("brand"),
                        product.get("amount"), product.get("quantity_value"),
                        product.get("quantity_unit"), product.get("description"),
                        product.get("image_url"), product.get("url"), storefront,
                        depot_id, now, now,
                    ),
                )
                regular_price = product.get("old_price_minor")
                if regular_price is None:
                    regular_price = product.get("price_minor")
                self.connection.execute(
                    """
                    INSERT INTO offers(
                        run_id, storefront, depot_id, product_id, category_path,
                        price_minor, regular_price_minor, price_card_minor,
                        discount_percent, promo, amount, unit_price,
                        unit_price_basis, in_stock, stock_qty, currency,
                        captured_at, extra_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, storefront, depot_id, product_id) DO UPDATE SET
                        category_path = COALESCE(offers.category_path, excluded.category_path),
                        price_minor = excluded.price_minor,
                        regular_price_minor = excluded.regular_price_minor,
                        price_card_minor = excluded.price_card_minor,
                        discount_percent = excluded.discount_percent,
                        promo = excluded.promo,
                        amount = excluded.amount,
                        unit_price = excluded.unit_price,
                        unit_price_basis = excluded.unit_price_basis,
                        in_stock = excluded.in_stock,
                        stock_qty = excluded.stock_qty,
                        currency = excluded.currency,
                        captured_at = excluded.captured_at,
                        extra_json = excluded.extra_json
                    """,
                    (
                        run_id, storefront, depot_id, product_id,
                        category_path or None, product.get("price_minor"), regular_price,
                        product.get("price_card_minor"), product.get("discount_percent"),
                        product.get("promo"), product.get("amount"),
                        product.get("unit_price"), product.get("unit_price_basis"),
                        int(bool(product.get("available", False))), product.get("stock_qty"),
                        product.get("currency") or "RUB", now,
                        _json(product.get("offer_extra", {})),
                    ),
                )
                self.connection.execute(
                    """
                    INSERT INTO offer_categories(
                        run_id, storefront, depot_id, product_id, category_id,
                        subcategory_ids_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, storefront, depot_id, product_id, category_id)
                    DO UPDATE SET subcategory_ids_json = excluded.subcategory_ids_json
                    """,
                    (
                        run_id, storefront, depot_id, product_id, task["category_id"],
                        _json(product.get("subcategory_ids", [])),
                    ),
                )
            self.connection.execute(
                """
                UPDATE category_tasks SET
                    status = 'complete', attempts = attempts + 1,
                    product_count = ?, captured_at = ?, error_json = NULL
                WHERE run_id = ? AND storefront = ? AND depot_id = ? AND category_id = ?
                """,
                (len(list(products)) if not isinstance(products, list) else len(products), now,
                 run_id, storefront, depot_id, task["category_id"]),
            )
            self.connection.commit()
        return added, updated

    def fail_category_task(
        self,
        run_id: str,
        storefront: str,
        depot_id: str,
        category_id: str,
        error: Any,
    ) -> None:
        with self._lock:
            self.connection.execute(
                """
                UPDATE category_tasks
                SET status = 'error', attempts = attempts + 1, error_json = ?
                WHERE run_id = ? AND storefront = ? AND depot_id = ? AND category_id = ?
                """,
                (_json(error), run_id, storefront, depot_id, category_id),
            )
            self.connection.commit()

    def finish_depot(self, run_id: str, storefront: str, depot_id: str) -> bool:
        with self._lock:
            counts = self.connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) AS completed,
                    COUNT(*) AS total
                FROM category_tasks
                WHERE run_id = ? AND storefront = ? AND depot_id = ?
                """,
                (run_id, storefront, depot_id),
            ).fetchone()
            complete = bool(counts and counts["total"] and counts["completed"] == counts["total"])
            product_count = self.connection.execute(
                """
                SELECT COUNT(*) FROM offers
                WHERE run_id = ? AND storefront = ? AND depot_id = ?
                """,
                (run_id, storefront, depot_id),
            ).fetchone()[0]
            self.connection.execute(
                """
                UPDATE depot_runs SET status = ?, completed_at = ?, product_count = ?
                WHERE run_id = ? AND storefront = ? AND depot_id = ?
                """,
                (
                    "complete" if complete else "partial",
                    utc_now() if complete else None,
                    product_count, run_id, storefront, depot_id,
                ),
            )
            self.connection.commit()
            return complete

    def pending_details(self, limit: int = 0) -> list[sqlite3.Row]:
        with self._lock:
            sql = """
                SELECT p.product_id, p.preferred_storefront AS storefront,
                       p.preferred_depot_id AS depot_id,
                       d.address, d.city, d.street, d.house,
                       d.latitude, d.longitude, d.currency
                FROM products p
                JOIN depots d
                  ON d.storefront = p.preferred_storefront
                 AND d.depot_id = p.preferred_depot_id
                WHERE p.service = ? AND p.detail_status != 'complete'
                ORDER BY p.product_id
            """
            params: list[Any] = [SERVICE]
            if limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            return list(self.connection.execute(sql, params))

    def save_detail(self, product_id: str, detail: Mapping[str, Any], raw: Any) -> None:
        with self._lock:
            self.connection.execute(
                """
                UPDATE products SET
                    name = COALESCE(?, name), brand = COALESCE(?, brand),
                    manufacturer = ?, country = ?, amount = COALESCE(?, amount),
                    quantity_value = COALESCE(?, quantity_value),
                    quantity_unit = COALESCE(?, quantity_unit),
                    kcal = ?, protein = ?, fat = ?, carbs = ?, composition = ?,
                    description = COALESCE(?, description), shelf_life = ?,
                    storage_conditions = ?, rating = ?, reviews_count = ?,
                    labels = ?, image_url = COALESCE(?, image_url),
                    url = COALESCE(?, url), detail_status = 'complete',
                    detail_attempts = detail_attempts + 1,
                    detail_error_json = NULL, raw_detail_json = ?, detailed_at = ?
                WHERE service = ? AND product_id = ?
                """,
                (
                    detail.get("name"), detail.get("brand"),
                    detail.get("manufacturer"), detail.get("country"),
                    detail.get("amount"), detail.get("quantity_value"),
                    detail.get("quantity_unit"), detail.get("kcal"),
                    detail.get("protein"), detail.get("fat"), detail.get("carbs"),
                    detail.get("composition"), detail.get("description"),
                    detail.get("shelf_life"), detail.get("storage_conditions"),
                    detail.get("rating"), detail.get("reviews_count"),
                    detail.get("labels"), detail.get("image_url"), detail.get("url"),
                    _json(raw), utc_now(), SERVICE, product_id,
                ),
            )
            self.connection.commit()

    def fail_detail(self, product_id: str, error: Any) -> None:
        with self._lock:
            self.connection.execute(
                """
                UPDATE products SET
                    detail_status = 'error', detail_attempts = detail_attempts + 1,
                    detail_error_json = ?
                WHERE service = ? AND product_id = ?
                """,
                (_json(error), SERVICE, product_id),
            )
            self.connection.commit()

    def summary(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            scalar = lambda sql, params=(): self.connection.execute(sql, params).fetchone()[0]
            depot_status = dict(self.connection.execute(
                "SELECT status, COUNT(*) FROM depot_runs WHERE run_id = ? GROUP BY status",
                (run_id,),
            ).fetchall())
            task_status = dict(self.connection.execute(
                "SELECT status, COUNT(*) FROM category_tasks WHERE run_id = ? GROUP BY status",
                (run_id,),
            ).fetchall())
            detail_status = dict(self.connection.execute(
                "SELECT detail_status, COUNT(*) FROM products GROUP BY detail_status"
            ).fetchall())
            return {
                "run_id": run_id,
                "depots": depot_status,
                "category_tasks": task_status,
                "offers": scalar("SELECT COUNT(*) FROM offers WHERE run_id = ?", (run_id,)),
                "unique_products_in_run": scalar(
                    "SELECT COUNT(DISTINCT product_id) FROM offers WHERE run_id = ?", (run_id,)
                ),
                "products_total": scalar("SELECT COUNT(*) FROM products"),
                "product_details": detail_status,
                "zones": scalar("SELECT COUNT(DISTINCT h3_index) FROM run_zone_depots WHERE run_id = ?", (run_id,)),
                "updated_at": utc_now(),
            }

    def run_status(self, run_id: str) -> str:
        with self._lock:
            row = self.connection.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return str(row["status"]) if row else "missing"
