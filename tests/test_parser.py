from __future__ import annotations

import unittest
from dataclasses import replace

from yandex_lavka.parser import LavkaClient, LavkaError, ResolvedLocation, YandexLavkaParser


LOCATION = ResolvedLocation(
    city="Москва",
    street="Тверская улица",
    house="1",
    longitude=37.6147157174475,
    latitude=55.75631021385086,
    building_id="public-test-building",
    depot_type="supermarket",
    depot_id="test-depot",
    currency="RUB",
    currency_sign="₽",
)


PRODUCT = {
    "id": "milk-1",
    "deepLink": "moloko-test-1-liter",
    "title": "Моло\u00adко тестовое",
    "longTitle": "Молоко тестовое 3,2%",
    "amount": "1,4 кг",
    "currentPrice": 118,
    "oldPrice": 169,
    "available": True,
    "options": {"pricePerCount": "1", "quantityType": "unit"},
}


def final_cart(quantity: str = "1") -> dict:
    quantity_number = int(quantity)
    return {
        "cartId": "cart-1",
        "cartVersion": 2,
        "availableForCheckout": True,
        "isSurge": False,
        "items": [
            {
                "id": "milk-1",
                "title": "Моло\u00adко тестовое",
                "subtitle": "Молоко тестовое 3,2%",
                "quantity": quantity,
                "catalogPrice": "118",
                "fullCatalogPrice": "169",
                "currency": "RUB",
                "isUnavailableOnDepot": False,
            }
        ],
        "price": {
            "items": {"number": 118 * quantity_number},
            "full": {"number": 169 * quantity_number},
            "total": {"number": 118 * quantity_number},
        },
        "orderConditions": {"deliveryCost": "0", "minEta": 10, "maxEta": 40},
        "requirements": {"sumToMinOrder": "0"},
    }


class FakeClient:
    def __init__(self, *, products=None, cart=None, personal_cart=None):
        self.products = products if products is not None else [PRODUCT]
        self.cart = cart if cart is not None else final_cart()
        self.personal_cart = personal_cart
        self.retrieve_count = 0
        self.updated_items = None
        self.resolved_input = None
        self.personal_logistic_selected = None

    def resolve_location(self, location):
        self.resolved_input = location
        if isinstance(location, dict) and location.get("depot_type"):
            depot_type = str(location["depot_type"])
            return replace(
                LOCATION,
                depot_type=depot_type,
                depot_id=f"test-{depot_type}-depot",
            )
        return LOCATION

    def search_products(self, query, location, limit):
        return {
            "layoutItems": [{"id": product["id"], "type": "good"} for product in self.products],
            "cacheProducts": self.products,
        }

    def get_products_by_id(self, product_ids, location):
        return {"products": [product for product in self.products if product["id"] in product_ids]}

    def retrieve_cart(self, location, cart_id=None):
        self.retrieve_count += 1
        if cart_id is None:
            return {
                "cartId": "cart-1",
                "cartVersion": 1,
                "nextIdempotencyToken": "next-token",
                "deliveryType": "eats_dispatch",
                "items": [],
                "price": {"items": {"number": 0}, "full": {"number": 0}, "total": {"number": 0}},
                "orderConditions": {"deliveryCost": "0"},
            }
        return self.cart

    def update_cart(self, location, cart, items):
        self.updated_items = items
        return self.cart

    def select_personal_logistic(self, location, cart, selected):
        self.personal_logistic_selected = selected
        if self.personal_cart is not None:
            self.cart = self.personal_cart
        return self.cart


class ParserTests(unittest.TestCase):
    def test_search_normalizes_money_amount_and_url(self):
        client = FakeClient()
        parser = YandexLavkaParser(client_factory=lambda: client)

        result = parser.search("молоко 3,2%", "Москва, Тверская улица, 1", limit=5)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["product_id"], "milk-1")
        self.assertEqual(result[0]["depot_type"], "supermarket")
        self.assertEqual(result[0]["storefront"], "Большая Лавка")
        self.assertEqual(result[0]["depot_id"], "test-depot")
        self.assertEqual(result[0]["name"], "Молоко тестовое 3,2%")
        self.assertEqual(result[0]["price"], 11800)
        self.assertEqual(result[0]["old_price"], 16900)
        self.assertEqual(result[0]["quantity_value"], 1.4)
        self.assertEqual(result[0]["quantity_unit"], "kg")
        self.assertEqual(
            result[0]["url"],
            "https://lavka.yandex.ru/supermarket/good/moloko-test-1-liter",
        )

    def test_search_can_select_regular_storefront(self):
        client = FakeClient()
        parser = YandexLavkaParser(client_factory=lambda: client)

        result = parser.search(
            "молоко",
            "Москва, Тверская улица, 1",
            limit=1,
            depot_type="regular",
        )

        self.assertEqual("regular", client.resolved_input["depot_type"])
        self.assertEqual("regular", result[0]["depot_type"])
        self.assertEqual("Лавка у дома", result[0]["storefront"])
        self.assertEqual("test-regular-depot", result[0]["depot_id"])
        self.assertEqual(
            "https://lavka.yandex.ru/good/moloko-test-1-liter",
            result[0]["url"],
        )

    def test_search_storefronts_keeps_results_separate(self):
        client = FakeClient()
        parser = YandexLavkaParser(client_factory=lambda: client)

        result = parser.search_storefronts(
            "молоко",
            "Москва, Тверская улица, 1",
            limit=1,
        )

        self.assertEqual({"regular", "supermarket"}, set(result))
        self.assertEqual("regular", result["regular"][0]["depot_type"])
        self.assertEqual("supermarket", result["supermarket"][0]["depot_type"])

    def test_invalid_storefront_is_rejected_locally(self):
        parser = YandexLavkaParser(client_factory=lambda: FakeClient())

        with self.assertRaisesRegex(ValueError, "depot_type must be one of"):
            parser.search("молоко", LOCATION, depot_type="lavka")

    def test_search_returns_empty_list_when_upstream_has_no_products(self):
        client = FakeClient(products=[])
        parser = YandexLavkaParser(client_factory=lambda: client)

        result = parser.search("несуществующий товар", LOCATION, limit=5)

        self.assertEqual([], result)

    def test_calculate_cart_returns_verified_total_and_discount(self):
        client = FakeClient()
        parser = YandexLavkaParser(client_factory=lambda: client)

        result = parser.calculate_cart(
            [{"product_id": "milk-1", "quantity": 1}],
            "Москва, Тверская улица, 1",
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["depot_type"], "supermarket")
        self.assertEqual(result["storefront"], "Большая Лавка")
        self.assertEqual(result["depot_id"], "test-depot")
        self.assertEqual(result["items_subtotal"], 16900)
        self.assertEqual(result["discounts"], 5100)
        self.assertEqual(result["delivery_fee"], 0)
        self.assertEqual(result["service_fees"], 0)
        self.assertEqual(result["total"], 11800)
        self.assertEqual(result["delivery_eta_minutes"], 40)
        self.assertEqual(client.retrieve_count, 2)
        self.assertEqual(client.updated_items[0]["price"], "118")

    def test_calculate_cart_separates_delivery_service_fee_and_minimum(self):
        cart = final_cart()
        cart["price"]["total"]["number"] = 177
        cart["orderConditions"]["deliveryCost"] = "49"
        cart["requirements"]["sumToMinOrder"] = "300"
        client = FakeClient(cart=cart)
        parser = YandexLavkaParser(client_factory=lambda: client)

        result = parser.calculate_cart(
            [{"product_id": "milk-1", "quantity": 1}],
            LOCATION,
        )

        self.assertEqual(4900, result["delivery_fee"])
        self.assertEqual(1000, result["service_fees"])
        self.assertEqual(5100, result["discounts"])
        self.assertEqual(30000, result["minimum_order_remaining"])
        self.assertEqual(17700, result["total"])
        self.assertEqual(
            result["total"],
            result["items_subtotal"]
            + result["delivery_fee"]
            + result["service_fees"]
            - result["discounts"],
        )

    def test_calculate_cart_returns_fast_and_slow_delivery_options(self):
        cart = final_cart()
        cart["logisticMethod"] = "onDemand"
        cart["onDemandDeliveryInfo"] = {
            "enabled": True,
            "pricingConditions": {"deliveryCost": 0, "minimalCartPrice": 0},
            "l10n": {"templateVars": {"fullDeliveryCostRange": "559"}},
            "eta": "30–60 мин",
            "isSurge": True,
            "tariff": "default",
        }
        cart["personalLogisticDeliveryInfo"] = {
            "enabled": True,
            "pricingConditions": {"deliveryCost": 0, "minimalCartPrice": 3000},
            "eta": "до 120 мин",
            "isSurge": True,
            "tariff": "pedestrian_later",
        }
        client = FakeClient(cart=cart)
        parser = YandexLavkaParser(client_factory=lambda: client)

        result = parser.calculate_cart(
            [{"product_id": "milk-1", "quantity": 1}],
            LOCATION,
        )

        self.assertEqual("on_demand", result["selected_delivery_option"])
        self.assertEqual(2, len(result["delivery_options"]))
        fast, slow = result["delivery_options"]
        self.assertEqual("on_demand", fast["code"])
        self.assertTrue(fast["selected"])
        self.assertEqual("confirmed", fast["pricing_status"])
        self.assertFalse(fast["requires_selection_recalculation"])
        self.assertEqual(0, fast["delivery_fee"])
        self.assertEqual(55900, fast["regular_delivery_fee"])
        self.assertEqual("30–60 мин", fast["eta_text"])
        self.assertEqual("personal", slow["code"])
        self.assertFalse(slow["selected"])
        self.assertEqual("preview", slow["pricing_status"])
        self.assertTrue(slow["requires_selection_recalculation"])
        self.assertEqual("до 120 мин", slow["eta_text"])
        self.assertEqual(0, slow["delivery_fee"])
        self.assertEqual(300000, slow["minimum_order"])
        self.assertEqual(288200, slow["minimum_order_remaining"])
        self.assertFalse(slow["available_for_checkout"])

    def test_personal_delivery_is_selected_and_recalculated(self):
        default_cart = final_cart()
        default_cart["logisticMethod"] = "onDemand"
        default_cart["onDemandDeliveryInfo"] = {
            "enabled": True,
            "pricingConditions": {"deliveryCost": 0, "minimalCartPrice": 0},
            "eta": "30–60 мин",
            "tariff": "default",
        }
        default_cart["personalLogisticDeliveryInfo"] = {
            "enabled": True,
            "pricingConditions": {"deliveryCost": 0, "minimalCartPrice": 3000},
            "eta": "до 120 мин",
            "tariff": "pedestrian_later",
        }
        personal_cart = final_cart()
        personal_cart["personalLogisticSelected"] = True
        personal_cart["logisticMethod"] = "onDemand"
        personal_cart["availableForCheckout"] = False
        personal_cart["orderConditions"] = {
            "deliveryCost": "0",
            "fullDeliveryCost": "0",
            "minimumOrderPrice": "4500",
            "eta": "до 120 мин",
            "minEta": 30,
            "maxEta": 120,
        }
        personal_cart["requirements"] = {"sumToMinOrder": "4281"}
        personal_cart["onDemandDeliveryInfo"] = default_cart["onDemandDeliveryInfo"]
        personal_cart["personalLogisticDeliveryInfo"] = default_cart[
            "personalLogisticDeliveryInfo"
        ]
        client = FakeClient(cart=default_cart, personal_cart=personal_cart)
        parser = YandexLavkaParser(client_factory=lambda: client)

        result = parser.calculate_cart(
            [{"product_id": "milk-1", "quantity": 1}],
            LOCATION,
            delivery_option="personal",
        )

        self.assertTrue(client.personal_logistic_selected)
        self.assertEqual("personal", result["selected_delivery_option"])
        self.assertEqual(450000, result["delivery_options"][1]["minimum_order"])
        self.assertEqual(428100, result["delivery_options"][1]["minimum_order_remaining"])
        self.assertEqual("confirmed", result["delivery_options"][1]["pricing_status"])
        self.assertEqual("до 120 мин", result["delivery_options"][1]["eta_text"])
        self.assertFalse(result["delivery_options"][1]["available_for_checkout"])

    def test_calculate_cart_options_confirms_both_delivery_modes_in_one_session(self):
        default_cart = final_cart()
        default_cart["logisticMethod"] = "onDemand"
        default_cart["onDemandDeliveryInfo"] = {
            "enabled": True,
            "pricingConditions": {"deliveryCost": 0, "minimalCartPrice": 0},
            "eta": "30–60 мин",
            "tariff": "default",
        }
        default_cart["personalLogisticDeliveryInfo"] = {
            "enabled": True,
            "pricingConditions": {"deliveryCost": 0, "minimalCartPrice": 3000},
            "eta": "до 120 мин",
            "tariff": "pedestrian_later",
        }
        personal_cart = final_cart()
        personal_cart["personalLogisticSelected"] = True
        personal_cart["logisticMethod"] = "onDemand"
        personal_cart["availableForCheckout"] = False
        personal_cart["orderConditions"] = {
            "deliveryCost": "0",
            "fullDeliveryCost": "0",
            "minimumOrderPrice": "4500",
            "eta": "до 120 мин",
            "minEta": 30,
            "maxEta": 120,
        }
        personal_cart["requirements"] = {"sumToMinOrder": "4281"}
        personal_cart["onDemandDeliveryInfo"] = default_cart["onDemandDeliveryInfo"]
        personal_cart["personalLogisticDeliveryInfo"] = default_cart[
            "personalLogisticDeliveryInfo"
        ]
        client = FakeClient(cart=default_cart, personal_cart=personal_cart)
        parser = YandexLavkaParser(client_factory=lambda: client)

        result = parser.calculate_cart_options(
            [{"product_id": "milk-1", "quantity": 1}],
            LOCATION,
        )

        self.assertEqual({"on_demand", "personal"}, set(result["quotes"]))
        self.assertEqual({}, result["errors"])
        self.assertEqual(
            "on_demand",
            result["quotes"]["on_demand"]["selected_delivery_option"],
        )
        self.assertEqual(
            "personal",
            result["quotes"]["personal"]["selected_delivery_option"],
        )
        self.assertEqual(
            450000,
            result["quotes"]["personal"]["delivery_options"][1]["minimum_order"],
        )
        self.assertTrue(client.personal_logistic_selected)
        self.assertEqual(3, client.retrieve_count)

    def test_calculate_cart_options_keeps_fast_quote_if_slow_mode_is_unavailable(self):
        client = FakeClient()
        parser = YandexLavkaParser(client_factory=lambda: client)

        result = parser.calculate_cart_options(
            [{"product_id": "milk-1", "quantity": 1}],
            LOCATION,
        )

        self.assertEqual({"on_demand"}, set(result["quotes"]))
        self.assertEqual("DELIVERY_UNAVAILABLE", result["errors"]["personal"]["code"])

    def test_unavailable_personal_delivery_is_reported(self):
        parser = YandexLavkaParser(client_factory=lambda: FakeClient())

        with self.assertRaises(LavkaError) as caught:
            parser.calculate_cart(
                [{"product_id": "milk-1", "quantity": 1}],
                LOCATION,
                delivery_option="personal",
            )

        self.assertEqual("DELIVERY_UNAVAILABLE", caught.exception.code)

    def test_calculate_cart_marks_missing_product_as_partial(self):
        client = FakeClient(products=[], cart={
            "cartId": "cart-1",
            "cartVersion": 1,
            "availableForCheckout": False,
            "items": [],
            "price": {"items": {"number": 0}, "full": {"number": 0}, "total": {"number": 0}},
            "orderConditions": {"deliveryCost": "0"},
            "requirements": {"sumToMinOrder": "0"},
        })
        parser = YandexLavkaParser(client_factory=lambda: client)

        result = parser.calculate_cart(
            [{"product_id": "missing-1", "quantity": 1}],
            "Москва, Тверская улица, 1",
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["unavailable_items"], [
            {"product_id": "missing-1", "reason": "PRODUCT_UNAVAILABLE"}
        ])
        self.assertEqual(result["total"], 0)

    def test_quantity_change_is_reported(self):
        client = FakeClient(cart=final_cart(quantity="2"))
        parser = YandexLavkaParser(client_factory=lambda: client)

        result = parser.calculate_cart(
            [{"product_id": "milk-1", "quantity": 3}],
            "Москва, Тверская улица, 1",
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["items"][0]["quantity"], 2)
        self.assertTrue(any("Quantity changed" in warning for warning in result["warnings"]))

    def test_duplicate_items_are_merged(self):
        client = FakeClient(cart=final_cart(quantity="2"))
        parser = YandexLavkaParser(client_factory=lambda: client)

        parser.calculate_cart(
            [
                {"product_id": "milk-1", "quantity": 1},
                {"product_id": "milk-1", "quantity": 1},
            ],
            "Москва, Тверская улица, 1",
        )

        self.assertEqual(client.updated_items[0]["quantity"], "2")

    def test_fractional_quantity_is_preserved(self):
        cart = final_cart()
        cart["items"][0]["quantity"] = "0.5"
        cart["price"] = {
            "items": {"number": 59},
            "full": {"number": 84.5},
            "total": {"number": 59},
        }
        client = FakeClient(cart=cart)
        parser = YandexLavkaParser(client_factory=lambda: client)

        result = parser.calculate_cart(
            [{"product_id": "milk-1", "quantity": 0.5}],
            LOCATION,
        )

        self.assertEqual("0.5", client.updated_items[0]["quantity"])
        self.assertEqual(0.5, result["items"][0]["quantity"])
        self.assertEqual(5900, result["items"][0]["line_total"])
        self.assertEqual("complete", result["status"])

    def test_non_positive_quantity_is_rejected_before_network_calls(self):
        parser = YandexLavkaParser(client_factory=lambda: FakeClient())

        with self.assertRaisesRegex(ValueError, "Quantity must be positive"):
            parser.calculate_cart(
                [{"product_id": "milk-1", "quantity": 0}],
                LOCATION,
            )


class ErrorMappingTests(unittest.TestCase):
    def test_rate_limit_preserves_retry_after(self):
        with self.assertRaises(LavkaError) as caught:
            LavkaClient._raise_http_error(429, "{}", "12")

        self.assertEqual("RATE_LIMITED", caught.exception.code)
        self.assertEqual("12", caught.exception.retry_after)

    def test_captcha_stops_processing(self):
        with self.assertRaises(LavkaError) as caught:
            LavkaClient._raise_http_error(403, '{"message":"captcha required"}', None)

        self.assertEqual("ANTI_BOT_CHALLENGE", caught.exception.code)

    def test_expired_csrf_is_reported_as_expired_session(self):
        with self.assertRaises(LavkaError) as caught:
            LavkaClient._raise_http_error(
                401,
                '{"data":{"name":"csrf","message":"token expired"}}',
                None,
            )

        self.assertEqual("SESSION_EXPIRED", caught.exception.code)

    def test_missing_depot_is_reported_as_unsupported_location(self):
        with self.assertRaises(LavkaError) as caught:
            LavkaClient._raise_http_error(
                400,
                '{"data":{"message":"DEPOT_NOT_FOUND"}}',
                None,
            )

        self.assertEqual("LOCATION_UNSUPPORTED", caught.exception.code)

    def test_server_error_is_normalized(self):
        with self.assertRaises(LavkaError) as caught:
            LavkaClient._raise_http_error(503, "temporary unavailable", None)

        self.assertEqual("UPSTREAM_UNAVAILABLE", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
