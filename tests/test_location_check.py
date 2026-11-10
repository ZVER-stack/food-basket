from __future__ import annotations

import unittest

from scripts.check_yandex_lavka_locations import _delivery_summary


class DeliverySummaryTests(unittest.TestCase):
    def test_current_nested_service_info_shape_is_normalized(self):
        summary = _delivery_summary(
            {
                "status": "open",
                "isOpen": True,
                "isDisabled": False,
                "isSurge": True,
                "deliveryTimeText": "15–45 мин",
                "defaultDeliveryMethod": "onDemand",
                "pricingConditions": {"deliveryCost": 0, "minimalCartPrice": 500},
                "deliveryConditions": [
                    {
                        "type": "delivery",
                        "templateVars": {
                            "deliveryCost": "0 ₽",
                            "fullDeliveryCost": "239 ₽",
                        },
                    }
                ],
                "deliveryMethods": {"onDemand": True, "timeSlots": True},
            }
        )

        self.assertEqual("open", summary["status"])
        self.assertTrue(summary["is_open"])
        self.assertTrue(summary["high_demand"])
        self.assertEqual("15–45 мин", summary["time_text"])
        self.assertEqual(0, summary["delivery_cost_rub"])
        self.assertEqual("239 ₽", summary["full_delivery_cost_text"])
        self.assertEqual(500, summary["minimum_order_rub"])
        self.assertTrue(summary["methods"]["timeSlots"])


if __name__ == "__main__":
    unittest.main()
