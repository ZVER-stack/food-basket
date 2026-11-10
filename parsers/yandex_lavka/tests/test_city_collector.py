import csv
import gzip
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.collect_yandex_lavka_city import extract_detail, unique_targets
from scripts.export_yandex_lavka_city_csv import COLUMNS, export
from scripts.lavka_city_store import CityCatalogStore
from yandex_lavka.parser import ResolvedLocation


def mapping_row(h3: str, storefront: str, depot_id: str, address: str) -> dict[str, str]:
    return {
        "h3_index": h3,
        "address": address,
        "address_lat": "55.7",
        "address_lon": "37.6",
        "address_in_zone": "true",
        "okrug": "ЦАО",
        "district": "Тестовый",
        "storefront": storefront,
        "depot_id": depot_id,
        "currency": "RUB",
        "service_status": "open",
        "delivery_eta_text": "15–45 мин",
        "delivery_fee_rub": "0",
        "min_order_rub": "0",
        "checked_at": "2026-10-20 10:00:00",
        "status": "ok",
        "error_code": "",
        "error_message": "",
    }


class CityCollectorTests(unittest.TestCase):
    def test_unique_targets_group_same_depot(self):
        rows = [
            mapping_row("h1", "regular", "d1", "Москва, Тверская улица, 1"),
            mapping_row("h2", "regular", "d1", "Москва, Тверская улица, 2"),
            mapping_row("h3", "supermarket", "d2", "Москва, Арбат, 1"),
        ]
        targets = unique_targets(rows, ("regular", "supermarket"))
        self.assertEqual(2, len(targets))
        self.assertEqual({("regular", "d1"), ("supermarket", "d2")}, {
            (target["storefront"], target["depot_id"]) for target in targets
        })

    def test_extract_detail_reads_nutrients_and_description_blocks(self):
        location = ResolvedLocation(
            city="Москва", street="Тверская улица", house="1",
            longitude=37.6, latitude=55.7, depot_type="regular", depot_id="d1",
        )
        response = {
            "product": {
                "id": "p1",
                "longTitle": "Молоко 3,2%",
                "amount": "930 мл",
                "currentPrice": 99,
                "available": True,
                "brand": "Простоквашино",
                "description": "Пастеризованное молоко",
                "descriptionBlocks": [
                    {"value": "Молоко нормализованное", "attributes": ["ingredientsLoc"]},
                    {"value": "7 сут, от +2 °C до +6 °C", "attributes": ["shelf_life_unit", "condition"]},
                    {"value": "АО Тест, Россия", "attributes": ["manufacturer", "countryISO"]},
                ],
                "options": {"ingredients": {"pfcTraits": [
                    {"id": "calories", "measures": {"per100g": "58"}},
                    {"id": "protein", "measures": {"per100g": "3,0"}},
                    {"id": "fat", "measures": {"per100g": "3,2"}},
                    {"id": "carbohydrate", "measures": {"per100g": "4,7"}},
                ]}},
            }
        }
        detail = extract_detail(response, location)
        self.assertEqual("Молоко нормализованное", detail["composition"])
        self.assertEqual("7 сут", detail["shelf_life"])
        self.assertEqual("от +2 °C до +6 °C", detail["storage_conditions"])
        self.assertEqual("АО Тест", detail["manufacturer"])
        self.assertEqual("Россия", detail["country"])
        self.assertEqual(58.0, detail["kcal"])
        self.assertEqual(3.2, detail["fat"])

    def test_store_deduplicates_details_and_export_repeats_depot_for_zones(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "city.sqlite3"
            rows = [
                mapping_row("h1", "regular", "d1", "Москва, Тверская улица, 1"),
                mapping_row("h2", "regular", "d1", "Москва, Тверская улица, 2"),
                mapping_row("h3", "regular", "d2", "Москва, Арбат, 1"),
            ]
            targets = unique_targets(rows, ("regular",))
            product = {
                "product_id": "p1", "name": "Молоко", "brand": "Бренд",
                "amount": "1 л", "quantity_value": 1, "quantity_unit": "l",
                "price_minor": 9900, "old_price_minor": 10900,
                "currency": "RUB", "available": True,
                "description": None, "image_url": None, "url": "https://example.test/p1",
                "subcategory_ids": [], "offer_extra": {},
            }
            task = {
                "category_id": "c1", "group_title": "Молочный прилавок",
                "title": "Молоко",
            }
            with CityCatalogStore(database) as store:
                run_id, _ = store.create_or_resume_run("hash", "scope", resume=False)
                store.register_mapping(run_id, rows, targets)
                categories = [{
                    "category_id": "c1", "group_id": "g1",
                    "group_title": "Молочный прилавок", "title": "Молоко",
                    "slug": "milk", "is_food": True, "raw": {},
                }]
                for target in targets:
                    store.save_layout(run_id, "regular", target["depot_id"], categories)
                    store.save_category_result(run_id, "regular", target["depot_id"], task, [product])
                summary = store.summary(run_id)
                self.assertEqual(1, summary["products_total"])
                self.assertEqual(2, summary["offers"])
                store.save_detail("p1", {
                    "name": "Молоко", "brand": "Бренд", "composition": "молоко",
                    "kcal": 58, "protein": 3, "fat": 3.2, "carbs": 4.7,
                }, {"product": {"id": "p1"}})

            output = root / "result.csv.gz"
            result = export(database, output, run_id)
            self.assertEqual(3, result["rows"])
            with gzip.open(output, "rt", encoding="utf-8", newline="") as source:
                exported = list(csv.DictReader(source))
            self.assertEqual(COLUMNS, list(exported[0]))
            self.assertEqual({"h1", "h2", "h3"}, {row["h3_index"] for row in exported})
            self.assertTrue(all(row["product_id"] == "p1" for row in exported))
            self.assertTrue(all(row["composition"] == "молоко" for row in exported))


if __name__ == "__main__":
    unittest.main()
