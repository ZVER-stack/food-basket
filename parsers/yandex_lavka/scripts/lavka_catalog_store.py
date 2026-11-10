"""SQLite storage and checkpoints for the Yandex Lavka catalogue collector."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return f"lavka-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class CatalogStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "CatalogStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                address TEXT NOT NULL,
                layout_slug TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                last_error_json TEXT
            );

            CREATE TABLE IF NOT EXISTS storefronts (
                run_id TEXT NOT NULL,
                depot_type TEXT NOT NULL,
                depot_id TEXT,
                city TEXT NOT NULL,
                street TEXT NOT NULL,
                house TEXT NOT NULL,
                currency TEXT NOT NULL,
                category_count INTEGER NOT NULL DEFAULT 0,
                product_count INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (run_id, depot_type),
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS categories (
                run_id TEXT NOT NULL,
                depot_type TEXT NOT NULL,
                category_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                group_title TEXT,
                title TEXT,
                slug TEXT,
                available INTEGER NOT NULL DEFAULT 1,
                is_food INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending',
                product_count INTEGER NOT NULL DEFAULT 0,
                captured_at TEXT,
                error_json TEXT,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (run_id, depot_type, category_id),
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS products (
                service TEXT NOT NULL,
                depot_type TEXT NOT NULL,
                depot_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                name TEXT NOT NULL,
                brand TEXT,
                amount TEXT,
                quantity_value REAL,
                quantity_unit TEXT,
                price_minor INTEGER,
                old_price_minor INTEGER,
                currency TEXT NOT NULL,
                available INTEGER NOT NULL,
                slug TEXT,
                url TEXT,
                image_url TEXT,
                description TEXT,
                attributes_json TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                first_captured_at TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                detail_status TEXT NOT NULL DEFAULT 'pending',
                detail_error_json TEXT,
                PRIMARY KEY (service, depot_type, depot_id, product_id)
            );

            CREATE TABLE IF NOT EXISTS product_categories (
                run_id TEXT NOT NULL,
                depot_type TEXT NOT NULL,
                depot_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                category_id TEXT NOT NULL,
                subcategory_ids_json TEXT NOT NULL,
                PRIMARY KEY (run_id, depot_type, depot_id, product_id, category_id),
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_categories_pending
                ON categories(run_id, depot_type, is_food, status);
            CREATE INDEX IF NOT EXISTS idx_products_details
                ON products(depot_type, depot_id, detail_status);
            """
        )
        self.connection.commit()

    def create_run(self, address: str, layout_slug: str, run_id: str | None = None) -> str:
        run_id = run_id or new_run_id()
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO runs(run_id, address, layout_slug, status, started_at, updated_at)
            VALUES (?, ?, ?, 'running', ?, ?)
            """,
            (run_id, address, layout_slug, now, now),
        )
        self.connection.commit()
        return run_id

    def latest_resumable_run(self, address: str, layout_slug: str) -> str | None:
        row = self.connection.execute(
            """
            SELECT run_id FROM runs
            WHERE address = ? AND layout_slug = ? AND status != 'complete'
            ORDER BY started_at DESC LIMIT 1
            """,
            (address, layout_slug),
        ).fetchone()
        return str(row["run_id"]) if row else None

    def set_run_status(self, run_id: str, status: str, error: Any = None) -> None:
        now = utc_now()
        completed = now if status == "complete" else None
        self.connection.execute(
            """
            UPDATE runs SET status = ?, updated_at = ?, completed_at = ?, last_error_json = ?
            WHERE run_id = ?
            """,
            (status, now, completed, _json(error) if error is not None else None, run_id),
        )
        self.connection.commit()

    def save_storefront(self, run_id: str, location: Any) -> None:
        self.connection.execute(
            """
            INSERT INTO storefronts(
                run_id, depot_type, depot_id, city, street, house, currency
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, depot_type) DO UPDATE SET
                depot_id = excluded.depot_id,
                city = excluded.city,
                street = excluded.street,
                house = excluded.house,
                currency = excluded.currency
            """,
            (
                run_id,
                location.depot_type,
                location.depot_id,
                location.city,
                location.street,
                location.house,
                location.currency,
            ),
        )
        self.connection.commit()

    def save_categories(
        self,
        run_id: str,
        depot_type: str,
        categories: Iterable[Mapping[str, Any]],
    ) -> None:
        for category in categories:
            self.connection.execute(
                """
                INSERT INTO categories(
                    run_id, depot_type, category_id, group_id, group_title,
                    title, slug, available, is_food, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, depot_type, category_id) DO UPDATE SET
                    group_id = excluded.group_id,
                    group_title = excluded.group_title,
                    title = excluded.title,
                    slug = excluded.slug,
                    available = excluded.available,
                    is_food = excluded.is_food,
                    raw_json = excluded.raw_json
                """,
                (
                    run_id,
                    depot_type,
                    category["category_id"],
                    category["group_id"],
                    category.get("group_title"),
                    category.get("title"),
                    category.get("slug"),
                    int(bool(category.get("available", True))),
                    int(bool(category.get("is_food", True))),
                    _json(category.get("raw", {})),
                ),
            )
        self.connection.commit()

    def pending_categories(self, run_id: str, depot_type: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM categories
                WHERE run_id = ? AND depot_type = ? AND is_food = 1 AND status != 'complete'
                ORDER BY group_title, title, category_id
                """,
                (run_id, depot_type),
            )
        )

    def finish_category(
        self,
        run_id: str,
        depot_type: str,
        category_id: str,
        product_count: int,
    ) -> None:
        self.connection.execute(
            """
            UPDATE categories
            SET status = 'complete', product_count = ?, captured_at = ?, error_json = NULL
            WHERE run_id = ? AND depot_type = ? AND category_id = ?
            """,
            (product_count, utc_now(), run_id, depot_type, category_id),
        )
        self.connection.commit()

    def fail_category(
        self,
        run_id: str,
        depot_type: str,
        category_id: str,
        error: Any,
    ) -> None:
        self.connection.execute(
            """
            UPDATE categories SET status = 'error', error_json = ?
            WHERE run_id = ? AND depot_type = ? AND category_id = ?
            """,
            (_json(error), run_id, depot_type, category_id),
        )
        self.connection.commit()

    def save_products(
        self,
        run_id: str,
        depot_type: str,
        depot_id: str,
        category_id: str,
        products: Iterable[Mapping[str, Any]],
    ) -> tuple[int, int]:
        added = 0
        updated = 0
        now = utc_now()
        for product in products:
            key = ("yandex_lavka", depot_type, depot_id, product["product_id"])
            exists = self.connection.execute(
                """
                SELECT 1 FROM products
                WHERE service = ? AND depot_type = ? AND depot_id = ? AND product_id = ?
                """,
                key,
            ).fetchone()
            first = now
            if exists:
                first_row = self.connection.execute(
                    """
                    SELECT first_captured_at FROM products
                    WHERE service = ? AND depot_type = ? AND depot_id = ? AND product_id = ?
                    """,
                    key,
                ).fetchone()
                first = str(first_row["first_captured_at"])
                updated += 1
            else:
                added += 1
            self.connection.execute(
                """
                INSERT INTO products(
                    service, depot_type, depot_id, product_id, name, brand, amount,
                    quantity_value, quantity_unit, price_minor, old_price_minor,
                    currency, available, slug, url, image_url, description,
                    attributes_json, raw_json, first_captured_at, captured_at,
                    detail_status, detail_error_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(service, depot_type, depot_id, product_id) DO UPDATE SET
                    name = excluded.name,
                    brand = COALESCE(excluded.brand, products.brand),
                    amount = excluded.amount,
                    quantity_value = excluded.quantity_value,
                    quantity_unit = excluded.quantity_unit,
                    price_minor = excluded.price_minor,
                    old_price_minor = excluded.old_price_minor,
                    currency = excluded.currency,
                    available = excluded.available,
                    slug = excluded.slug,
                    url = excluded.url,
                    image_url = COALESCE(excluded.image_url, products.image_url),
                    description = COALESCE(excluded.description, products.description),
                    attributes_json = excluded.attributes_json,
                    raw_json = excluded.raw_json,
                    captured_at = excluded.captured_at,
                    detail_status = CASE
                        WHEN products.detail_status = 'complete' THEN 'complete'
                        ELSE excluded.detail_status
                    END
                """,
                (
                    *key,
                    product["name"],
                    product.get("brand"),
                    product.get("amount"),
                    product.get("quantity_value"),
                    product.get("quantity_unit"),
                    product.get("price_minor"),
                    product.get("old_price_minor"),
                    product.get("currency", "RUB"),
                    int(bool(product.get("available", False))),
                    product.get("slug"),
                    product.get("url"),
                    product.get("image_url"),
                    product.get("description"),
                    _json(product.get("attributes", {})),
                    _json(product.get("raw", {})),
                    first,
                    now,
                    product.get("detail_status", "pending"),
                ),
            )
            self.connection.execute(
                """
                INSERT INTO product_categories(
                    run_id, depot_type, depot_id, product_id, category_id,
                    subcategory_ids_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, depot_type, depot_id, product_id, category_id)
                DO UPDATE SET subcategory_ids_json = excluded.subcategory_ids_json
                """,
                (
                    run_id,
                    depot_type,
                    depot_id,
                    product["product_id"],
                    category_id,
                    _json(product.get("subcategory_ids", [])),
                ),
            )
        self.connection.commit()
        return added, updated

    def pending_details(
        self,
        depot_type: str,
        depot_id: str,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        sql = """
            SELECT * FROM products
            WHERE service = 'yandex_lavka' AND depot_type = ? AND depot_id = ?
              AND detail_status != 'complete'
            ORDER BY product_id
        """
        parameters: list[Any] = [depot_type, depot_id]
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        return list(self.connection.execute(sql, parameters))

    def save_detail(
        self,
        depot_type: str,
        depot_id: str,
        product: Mapping[str, Any],
    ) -> None:
        self.connection.execute(
            """
            UPDATE products SET
                name = ?, brand = ?, amount = ?, quantity_value = ?, quantity_unit = ?,
                price_minor = ?, old_price_minor = ?, available = ?, slug = ?, url = ?,
                image_url = ?, description = ?, attributes_json = ?, raw_json = ?,
                captured_at = ?, detail_status = 'complete', detail_error_json = NULL
            WHERE service = 'yandex_lavka' AND depot_type = ? AND depot_id = ?
              AND product_id = ?
            """,
            (
                product["name"],
                product.get("brand"),
                product.get("amount"),
                product.get("quantity_value"),
                product.get("quantity_unit"),
                product.get("price_minor"),
                product.get("old_price_minor"),
                int(bool(product.get("available", False))),
                product.get("slug"),
                product.get("url"),
                product.get("image_url"),
                product.get("description"),
                _json(product.get("attributes", {})),
                _json(product.get("raw", {})),
                utc_now(),
                depot_type,
                depot_id,
                product["product_id"],
            ),
        )
        self.connection.commit()

    def fail_detail(
        self,
        depot_type: str,
        depot_id: str,
        product_id: str,
        error: Any,
    ) -> None:
        self.connection.execute(
            """
            UPDATE products SET detail_status = 'error', detail_error_json = ?
            WHERE service = 'yandex_lavka' AND depot_type = ? AND depot_id = ?
              AND product_id = ?
            """,
            (_json(error), depot_type, depot_id, product_id),
        )
        self.connection.commit()

    def update_storefront_counts(self, run_id: str, depot_type: str) -> None:
        category_count = self.connection.execute(
            """
            SELECT COUNT(*) FROM categories
            WHERE run_id = ? AND depot_type = ? AND is_food = 1 AND status = 'complete'
            """,
            (run_id, depot_type),
        ).fetchone()[0]
        product_count = self.connection.execute(
            """
            SELECT COUNT(DISTINCT product_id) FROM product_categories
            WHERE run_id = ? AND depot_type = ?
            """,
            (run_id, depot_type),
        ).fetchone()[0]
        pending = self.connection.execute(
            """
            SELECT COUNT(*) FROM categories
            WHERE run_id = ? AND depot_type = ? AND is_food = 1 AND status != 'complete'
            """,
            (run_id, depot_type),
        ).fetchone()[0]
        self.connection.execute(
            """
            UPDATE storefronts SET category_count = ?, product_count = ?, completed = ?
            WHERE run_id = ? AND depot_type = ?
            """,
            (category_count, product_count, int(pending == 0), run_id, depot_type),
        )
        self.connection.commit()

    def categories_complete(self, run_id: str, depot_type: str) -> bool:
        row = self.connection.execute(
            """
            SELECT
                SUM(CASE WHEN is_food = 1 THEN 1 ELSE 0 END) AS total,
                SUM(CASE WHEN is_food = 1 AND status = 'complete' THEN 1 ELSE 0 END) AS completed
            FROM categories WHERE run_id = ? AND depot_type = ?
            """,
            (run_id, depot_type),
        ).fetchone()
        return bool(row and row["total"] and row["total"] == row["completed"])

    def mark_unseen_unavailable(
        self,
        run_id: str,
        depot_type: str,
        depot_id: str,
    ) -> int:
        """Keep known sold-out goods and mark those absent from a complete snapshot."""

        observed = self.connection.execute(
            """
            SELECT COUNT(DISTINCT product_id) FROM product_categories
            WHERE run_id = ? AND depot_type = ? AND depot_id = ?
            """,
            (run_id, depot_type, depot_id),
        ).fetchone()[0]
        if not observed:
            return 0
        cursor = self.connection.execute(
            """
            UPDATE products
            SET available = 0, captured_at = ?
            WHERE service = 'yandex_lavka' AND depot_type = ? AND depot_id = ?
              AND product_id NOT IN (
                  SELECT product_id FROM product_categories
                  WHERE run_id = ? AND depot_type = ? AND depot_id = ?
              )
            """,
            (utc_now(), depot_type, depot_id, run_id, depot_type, depot_id),
        )
        self.connection.commit()
        return max(cursor.rowcount, 0)

    def summary(self, run_id: str) -> dict[str, Any]:
        run = self.connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        storefronts = list(
            self.connection.execute(
                "SELECT * FROM storefronts WHERE run_id = ? ORDER BY depot_type", (run_id,)
            )
        )
        details = self.connection.execute(
            """
            SELECT p.detail_status, COUNT(DISTINCT p.depot_type || ':' || p.depot_id || ':' || p.product_id) AS count
            FROM products AS p
            JOIN product_categories AS pc
              ON pc.depot_type = p.depot_type
             AND pc.depot_id = p.depot_id
             AND pc.product_id = p.product_id
            WHERE p.service = 'yandex_lavka' AND pc.run_id = ?
            GROUP BY p.detail_status ORDER BY p.detail_status
            """,
            (run_id,),
        )
        return {
            "run": dict(run) if run else None,
            "storefronts": [dict(row) for row in storefronts],
            "details": {str(row["detail_status"]): int(row["count"]) for row in details},
        }
