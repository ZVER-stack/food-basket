from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.collect_yandex_lavka_categories import extract_categories, extract_products
from scripts.lavka_catalog_store import CatalogStore
from yandex_lavka.parser import ResolvedLocation


LOCATION = ResolvedLocation(
    city="Москва",
    street="Рязанский проспект",
    house="2к2",
    longitude=37.663,
    latitude=55.733,
    depot_type="regular",
    depot_id="depot-1",
)


class CategoryNormalizationTest(unittest.TestCase):
    def test_tree_keeps_non_food_metadata_but_marks_it_for_skipping(self):
        layout = {
            "sections": [
                {
                    "categoryGroup": {
                        "categoryGroupInfo": {"id": "dairy", "title": "Молочный прилавок"}
                    },
                    "categories": [
                        {
                            "categoryInfo": {
                                "id": "milk",
                                "title": "Молоко",
                                "deepLink": "milk",
                                "available": True,
                            }
                        }
                    ],
                },
                {
                    "categoryGroup": {
                        "categoryGroupInfo": {"id": "pharmacy", "title": "Аптека"}
                    },
                    "categories": [
                        {"categoryInfo": {"id": "vitamins", "title": "Витамины"}}
                    ],
                },
            ]
        }

        categories = extract_categories(layout)

        self.assertEqual(2, len(categories))
        self.assertTrue(categories[0]["is_food"])
        self.assertFalse(categories[1]["is_food"])

    def test_category_products_include_unavailable_and_subcategory_path(self):
        response = {
            "categories": [
                {
                    "value": {"type": "category", "id": "milk"},
                    "items": [
                        {
                            "value": {"type": "subcategory", "id": "pasteurized"},
                            "items": [
                                {"value": {"type": "good", "id": "milk-1"}}
                            ],
                        }
                    ],
                }
            ],
            "products": [
                {
                    "id": "milk-1",
                    "longTitle": "Молоко тестовое 3,2%",
                    "amount": "930 мл",
                    "currentPrice": 120,
                    "oldPrice": 150,
                    "available": False,
                    "deepLink": "milk-test",
                    "options": {"ingredients": {"composition": "молоко"}},
                },
                {"id": "tile-1", "type": "subcategory", "title": "Пастеризованное"},
            ],
        }

        products = extract_products(response, LOCATION)

        self.assertEqual(1, len(products))
        self.assertEqual("milk-1", products[0]["product_id"])
        self.assertEqual(12000, products[0]["price_minor"])
        self.assertFalse(products[0]["available"])
        self.assertEqual(["pasteurized"], products[0]["subcategory_ids"])
        self.assertEqual("ml", products[0]["quantity_unit"])
        self.assertIn("options", products[0]["attributes"])


class CatalogStoreTest(unittest.TestCase):
    def test_sqlite_checkpoint_can_resume_incomplete_category(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.sqlite3"
            with CatalogStore(path) as store:
                run_id = store.create_run("Москва, Рязанский проспект, 2к2", "grocery")
                store.save_storefront(run_id, LOCATION)
                store.save_categories(
                    run_id,
                    "regular",
                    [
                        {
                            "category_id": "milk",
                            "group_id": "dairy",
                            "group_title": "Молочный прилавок",
                            "title": "Молоко",
                            "available": True,
                            "is_food": True,
                            "raw": {"id": "milk"},
                        }
                    ],
                )
                self.assertEqual(1, len(store.pending_categories(run_id, "regular")))

            with CatalogStore(path) as reopened:
                self.assertEqual(
                    run_id,
                    reopened.latest_resumable_run(
                        "Москва, Рязанский проспект, 2к2", "grocery"
                    ),
                )
                reopened.finish_category(run_id, "regular", "milk", 0)
                self.assertEqual([], reopened.pending_categories(run_id, "regular"))

    def test_complete_new_snapshot_marks_disappeared_product_unavailable(self):
        def product(product_id: str) -> dict:
            return {
                "product_id": product_id,
                "name": f"Товар {product_id}",
                "price_minor": 10000,
                "currency": "RUB",
                "available": True,
                "attributes": {},
                "raw": {"id": product_id},
                "subcategory_ids": [],
            }

        with tempfile.TemporaryDirectory() as directory:
            with CatalogStore(Path(directory) / "catalog.sqlite3") as store:
                first_run = store.create_run("Москва", "grocery")
                store.save_products(
                    first_run,
                    "regular",
                    "depot-1",
                    "food",
                    [product("one"), product("two")],
                )
                second_run = store.create_run("Москва", "grocery")
                store.save_products(
                    second_run,
                    "regular",
                    "depot-1",
                    "food",
                    [product("one")],
                )

                self.assertEqual(
                    1,
                    store.mark_unseen_unavailable(second_run, "regular", "depot-1"),
                )
                availability = {
                    row["product_id"]: row["available"]
                    for row in store.connection.execute(
                        "SELECT product_id, available FROM products ORDER BY product_id"
                    )
                }
                self.assertEqual({"one": 1, "two": 0}, availability)


if __name__ == "__main__":
    unittest.main()
