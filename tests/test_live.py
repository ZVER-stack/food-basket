from __future__ import annotations

import os
import unittest

from yandex_lavka import YandexLavkaParser


@unittest.skipUnless(os.getenv("LAVKA_LIVE_TEST") == "1", "set LAVKA_LIVE_TEST=1 to call Lavka")
class LiveLavkaTests(unittest.TestCase):
    def test_public_address_search_and_cart(self):
        parser = YandexLavkaParser()
        address = "Москва, Тверская улица, 1"
        products = parser.search("молоко 3,2%", address, limit=3)
        self.assertTrue(products)
        self.assertTrue(products[0]["available"])

        cart = parser.calculate_cart(
            [{"product_id": products[0]["product_id"], "quantity": 1}],
            address,
        )
        self.assertIn(cart["status"], {"complete", "partial"})
        self.assertGreater(cart["total"], 0)
        self.assertEqual(
            cart["total"],
            cart["items_subtotal"]
            + cart["delivery_fee"]
            + cart["service_fees"]
            - cart["discounts"],
        )
        self.assertTrue(cart["delivery_options"])
        self.assertTrue(any(option["selected"] for option in cart["delivery_options"]))


if __name__ == "__main__":
    unittest.main()
