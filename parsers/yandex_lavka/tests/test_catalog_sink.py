import tempfile
import unittest
from pathlib import Path

from scripts.catalog_sink import merge_products


class CatalogSinkTest(unittest.TestCase):
    def test_merge_deduplicates_service_product_id_and_keeps_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            first = {
                "query": "молоко",
                "page_url": "https://example.test/search?q=milk",
                "category_hint": "молоко",
                "products": [
                    {"product_id": "10", "name": "Молоко 3,2% 1 л", "url": "https://example.test/10"}
                ],
            }
            second = {
                **first,
                "query": "молоко 3,2",
                "products": [
                    {"product_id": "10", "name": "Молоко 3,2% 1 л", "url": "https://example.test/10"},
                    {"product_id": "11", "name": "Молоко 2,5% 1 л", "url": "https://example.test/11"},
                ],
            }

            self.assertEqual((1, 0), merge_products(output, "test_service", first))
            self.assertEqual((1, 1), merge_products(output, "test_service", second))

            saved = __import__("json").loads((output / "test_service.json").read_text(encoding="utf-8"))
            self.assertEqual(2, len(saved))
            product = next(item for item in saved if item["product_id"] == "10")
            self.assertEqual(["молоко", "молоко 3,2"], product["observed_queries"])


if __name__ == "__main__":
    unittest.main()
