from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from map_yandex_lavka_hexagon_depots import parse_address_parts, read_hexagons, summarize


class AddressParsingTest(unittest.TestCase):
    def test_keeps_comma_inside_street_name(self) -> None:
        self.assertEqual(
            parse_address_parts("Москва, МКАД, 14-й километр, 2 к1"),
            ("Москва", "МКАД, 14-й километр", "2 к1"),
        )

    def test_rejects_incomplete_address(self) -> None:
        with self.assertRaises(ValueError):
            parse_address_parts("Москва, Кремль")


class HexagonInputTest(unittest.TestCase):
    def test_rejects_duplicate_h3_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hexagons.csv"
            fields = [
                "h3_index", "address", "address_lat", "address_lon",
                "address_in_zone", "okrug", "district",
            ]
            with path.open("w", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=fields)
                writer.writeheader()
                for _ in range(2):
                    writer.writerow({
                        "h3_index": "same",
                        "address": "Москва, Тверская улица, 1",
                        "address_lat": "55.0",
                        "address_lon": "37.0",
                        "address_in_zone": "True",
                        "okrug": "ЦАО",
                        "district": "Тверской",
                    })
            with self.assertRaises(ValueError):
                read_hexagons(path)


class SummaryTest(unittest.TestCase):
    def test_counts_unique_storefront_depots(self) -> None:
        zones = [{"h3_index": "a"}, {"h3_index": "b"}, {"h3_index": "c"}]
        rows = {
            ("a", "regular"): {"h3_index": "a", "storefront": "regular", "depot_id": "r1", "status": "ok"},
            ("b", "regular"): {"h3_index": "b", "storefront": "regular", "depot_id": "r1", "status": "ok"},
            ("c", "regular"): {"h3_index": "c", "storefront": "regular", "depot_id": "r2", "status": "ok"},
            ("a", "supermarket"): {"h3_index": "a", "storefront": "supermarket", "depot_id": "s1", "status": "ok"},
            ("b", "supermarket"): {"h3_index": "b", "storefront": "supermarket", "depot_id": "s1", "status": "ok"},
            ("c", "supermarket"): {"h3_index": "c", "storefront": "supermarket", "depot_id": "s1", "status": "ok"},
        }
        result = summarize(zones, rows, ("regular", "supermarket"))
        self.assertEqual(result["unique_depots_by_storefront"], {"regular": 2, "supermarket": 1})
        self.assertEqual(result["unique_depot_storefront_pairs"], 3)
        self.assertEqual(result["unique_complete_depot_profiles"], 2)
        self.assertEqual(result["supported_zone_storefront_pairs"], 6)
        self.assertEqual(result["zone_coverage"], {"all_storefronts": 3})
        self.assertEqual(result["catalog_run_reduction_percent"], 50.0)
        self.assertEqual(result["catalog_run_reduction_from_supported_percent"], 50.0)


if __name__ == "__main__":
    unittest.main()
