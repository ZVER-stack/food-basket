"""Offline regression tests. Synthetic catalog fixtures are NOT live Ozon evidence."""
import csv
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

import ozon_fresh_fast as f


ZONE = {"h3_index": "8711aa632ffffff", "address": "Москва, Бакунинская улица, 60/2", "address_lat": "55.777199", "address_lon": "37.690003", "address_in_zone": "True"}
CAT = {"category_id": "milk-fixture", "name": "Продукты питания > Молоко", "scope": "food", "verified_leaf": True, "url": f.HOST + "/category/milk-1/?miniapp=supermarket"}
MAPPING = {"product_id": "/sku", "name": "/name", "price": "/ordinary", "price_card": "/card", "price_regular": "/old", "url": "/url", "in_stock": "/available", "barcode": "/barcode"}
CARD = {"sku": 123, "name": "Молоко 3,2% 950 мл", "ordinary": "89,90 ₽", "card": "85 ₽", "old": "109 ₽", "url": "/product/moloko-123/", "available": True, "barcode": "4600000000001"}
SCHEMA = {"widget_prefix": "searchResultsV2-", "items": "/items", "next": "/nextPage"}
CONTEXT_SCHEMA = {"delivery_available": "/available", "lat": "/lat", "lon": "/lon", "store_id": "/warehouse", "delivery_fee": "/fee", "price_scope": "/scope"}
CONTEXT = {"available": True, "lat": float(ZONE["address_lat"]), "lon": float(ZONE["address_lon"]), "warehouse": "fixture-A", "fee": 0, "scope": "offer-A"}


def page(items, next_page=None):
    return {"widgetStates": {"searchResultsV2-fixture": json.dumps({"items": items})}, "nextPage": next_page}


def config():
    return {"verified_anonymous_contract": True, "fresh_only": True, "set_location": {"url": f.HOST + "/api/fixture/location", "method": "POST", "json": {"lat": "{lat}", "lon": "{lon}"}}, "context": {"url": f.HOST + "/api/fixture/context"}, "context_schema": CONTEXT_SCHEMA, "catalog_schema": SCHEMA, "card_mapping": MAPPING, "categories": [CAT]}


class ParsingTests(unittest.TestCase):
    def test_template_has_exactly_37_columns(self):
        self.assertEqual(len(f.COLUMNS), 37)
        self.assertEqual(len(set(f.COLUMNS)), 37)

    def test_numeric_currency_and_decimal_comma(self):
        self.assertEqual(f.number("1\u2009299,90 ₽"), "1299.90")
        self.assertEqual(f.number(0), "0")

    def test_numbers_do_not_swallow_ranges_or_descriptions(self):
        for text in ("100–200", "89 по карте", "NaN", "Infinity", "-1", True, "1e99999999999999999999"):
            with self.subTest(text=text), self.assertRaises(f.ParseError):
                f.number(text)

    def test_pointer_decodes_json_and_escaped_keys(self):
        self.assertEqual(f.pointer({"a": '{"x/y": [7]}'}, "/a/x~1y/0"), 7)
        self.assertIsNone(f.pointer({}, "/missing"))

    def test_amount_ordinary_and_card_prices_remain_separate(self):
        row = f.card_row(CARD, MAPPING, CAT)
        self.assertEqual((row["price"], row["price_card"], row["price_regular"]), ("89.90", "85", "109"))
        self.assertEqual(row["amount"], "950 мл")
        self.assertEqual(json.loads(row["extra"])["barcode"], CARD["barcode"])

    def test_pack_amount_not_fat_percentage(self):
        card = dict(CARD, name="Йогурт 2,5% 6 шт × 100 г")
        self.assertEqual(f.card_row(card, MAPPING, CAT)["amount"], "6 шт × 100 г")

    def test_card_price_cannot_substitute_ordinary(self):
        with self.assertRaises(f.ParseError):
            f.card_row(dict(CARD, ordinary=None), MAPPING, CAT)

    def test_regular_falls_back_only_to_ordinary(self):
        row = f.card_row(dict(CARD, old=None), MAPPING, CAT)
        self.assertEqual(row["price_regular"], row["price"])

    def test_recommendations_not_read_as_catalog(self):
        with self.assertRaises(f.ParseError):
            f.catalog_page({"widgetStates": {"recommendations": {"items": [CARD]}}, "nextPage": None}, SCHEMA)

    def test_missing_next_field_is_not_terminal(self):
        data = page([CARD])
        del data["nextPage"]
        with self.assertRaises(f.ParseError):
            f.catalog_page(data, SCHEMA)

    def test_url_id_mismatch_rejected(self):
        with self.assertRaises(f.ParseError):
            f.card_row(dict(CARD, sku=456), MAPPING, CAT)

    def test_offsite_urls_rejected(self):
        for url in ("https://evil.example/test", "http://www.ozon.ru/api/a", "https://www.ozon.ru.evil.example/api/a", "https://user:pass@www.ozon.ru/api/a", "https://www.ozon.ru:8443/api/a"):
            with self.subTest(url=url), self.assertRaises(f.ParseError):
                f.public_url(url)

    def test_typed_house_coordinates_and_encoded_address(self):
        result = f.render({"lat": "{lat}", "url": "https://www.ozon.ru/api/test?q={address}"}, {"lat": 55.7, "address": "Москва & дом 1"})
        self.assertEqual(result["lat"], 55.7)
        self.assertIn("%26", result["url"])

    def test_house_coordinates_verified_not_zone_center(self):
        result = f.read_context(CONTEXT, CONTEXT_SCHEMA, ZONE)
        self.assertEqual(result["delivery_fee"], "0")
        with self.assertRaises(f.ParseError):
            f.read_context(dict(CONTEXT, lat=55.9), CONTEXT_SCHEMA, ZONE)

    def test_no_delivery_requires_explicit_boolean(self):
        with self.assertRaises(f.ParseError):
            f.read_context(dict(CONTEXT, available=None), CONTEXT_SCHEMA, ZONE)
        self.assertFalse(f.read_context(dict(CONTEXT, available=False, warehouse=None), CONTEXT_SCHEMA, ZONE)["delivery_available"])

    def test_optional_store_stays_blank_when_not_exposed(self):
        self.assertEqual(f.read_context(dict(CONTEXT, warehouse=None), CONTEXT_SCHEMA, ZONE)["store_id"], "")
        with self.assertRaises(f.ParseError):
            f.read_context(dict(CONTEXT, warehouse={"id": "ambiguous"}), CONTEXT_SCHEMA, ZONE)

    def test_unverified_or_broad_category_contract_rejected(self):
        f.check_contract(config())
        c = config()
        c["verified_anonymous_contract"] = False
        with self.assertRaises(f.ParseError):
            f.check_contract(c)
        c = config()
        c["categories"] = [dict(CAT, scope="cosmetics")]
        with self.assertRaises(f.ParseError):
            f.check_contract(c)


class TransportTests(unittest.TestCase):
    def test_403_429_and_redirect_stop_without_retry(self):
        for status in (307, 403, 429):
            with self.subTest(status=status):
                client = f.Client()
                client.opener = Mock()
                client.opener.open.side_effect = urllib.error.HTTPError(f.HOST, status, "test", {}, io.BytesIO(b""))
                with self.assertRaises(f.ProtectionError):
                    client.request({"url": f.HOST + "/api/fixture"})
                with self.assertRaises(f.ProtectionError):
                    client.request({"url": f.HOST + "/api/fixture"})
                self.assertEqual(client.opener.open.call_count, 1)

    def test_html_is_not_no_delivery(self):
        client = f.Client()
        response = Mock()
        response.read.return_value = b"<html>Antibot Challenge Page</html>"
        response.headers = {"Content-Type": "text/html"}
        client.opener = MagicMock()
        client.opener.open.return_value.__enter__.return_value = response
        with self.assertRaises(f.ProtectionError):
            client.request({"url": f.HOST + "/api/fixture"})

    def test_global_interval_and_request_budget(self):
        client = f.Client(interval=1.05, max_requests=2)
        response = Mock()
        response.read.return_value = b'{}'
        response.headers = {"Content-Type": "application/json"}
        client.opener = MagicMock()
        client.opener.open.return_value.__enter__.return_value = response
        with patch.object(f.time, "sleep") as sleep:
            client.request({"url": f.HOST + "/api/fixture"})
            client.request({"url": f.HOST + "/api/fixture"})
            self.assertGreater(sleep.call_args.args[0], 1)
            with self.assertRaises(f.BudgetError):
                client.request({"url": f.HOST + "/api/fixture"})
        self.assertEqual(client.opener.open.call_count, 2)


class StateAndCrawlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.tmp.name)
        self.state = f.State(self.root / "state.sqlite", "fixture")

    def tearDown(self):
        self.state.db.close()
        self.tmp.cleanup()

    def test_no_house_zones_excluded_and_invalid_flags_fail(self):
        path = self.root / "zones.csv"
        with path.open("w", encoding="utf-8", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=list(ZONE))
            writer.writeheader()
            writer.writerows([ZONE, dict(ZONE, h3_index="8711aa78dffffff", address_in_zone="False")])
        active, skipped = f.load_zones(path)
        self.assertEqual((len(active), len(skipped)), (1, 1))

    def test_failed_zone_does_not_export_as_no_delivery(self):
        self.state.mark(ZONE["h3_index"], "error", error="HTTP 403")
        path = self.root / "result.csv"
        self.assertEqual(self.state.export(path), 0)
        self.state.mark(ZONE["h3_index"], "no_delivery")
        self.assertEqual(self.state.export(path), 1)
        with path.open(encoding="utf-8", newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual([k for k,v in row.items() if v], ["service", "h3_index", "parsed_at"])

    def test_resume_idempotent_export(self):
        self.state.mark(ZONE["h3_index"], "complete", [f.card_row(CARD, MAPPING, CAT)])
        path = self.root / "result.csv"
        self.assertEqual(self.state.export(path), 1)
        self.assertEqual(self.state.export(path), 1)
        self.assertTrue(self.state.done(ZONE["h3_index"]))

    def test_changed_contract_cannot_resume_same_snapshot(self):
        with self.assertRaises(f.ParseError):
            f.State(self.root / "state.sqlite", "different")

    def test_paginated_catalog_deduplicated_and_page_cache_reused(self):
        client = Mock()
        next_url = CAT["url"] + "&page=2"
        client.request.side_effect = [page([CARD], next_url), page([CARD, dict(CARD, sku=456, url="/product/other-456/")])]
        _, rows = f.collect_catalog(client, config(), {"store_id": "A"}, self.state, "A")
        self.assertEqual(len(rows), 2)
        self.assertEqual(client.request.call_count, 2)
        other = Mock()
        _, rows2 = f.collect_catalog(other, config(), {"store_id": "A"}, self.state, "A")
        self.assertEqual(rows, rows2)
        other.request.assert_not_called()

    def test_pagination_loop_not_marked_complete(self):
        client = Mock()
        client.request.return_value = page([CARD], CAT["url"])
        with self.assertRaises(f.ParseError):
            f.collect_catalog(client, config(), {"store_id": "A"})

    def test_missing_price_or_empty_catalog_not_silently_exported(self):
        for cards in ([dict(CARD, ordinary=None)], []):
            client = Mock()
            client.request.return_value = page(cards)
            with self.subTest(cards=cards), self.assertRaises(f.ParseError):
                f.collect_catalog(client, config(), {"store_id": "A"})

    def test_different_prices_same_sku_rejected(self):
        client = Mock()
        client.request.return_value = page([CARD, dict(CARD, ordinary="99 ₽")])
        with self.assertRaises(f.ParseError):
            f.collect_catalog(client, config(), {"store_id": "A"})

    def test_end_to_end_two_zones_one_warehouse(self):
        zones_path = self.root / "zones.csv"
        with zones_path.open("w", encoding="utf-8", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=list(ZONE))
            writer.writeheader()
            writer.writerows([ZONE, dict(ZONE, h3_index="8711aa78dffffff")])
        contract_path = self.root / "contract.json"
        f.write_json(contract_path, config())
        mock_client = Mock()
        mock_client.request.side_effect = [{}, CONTEXT, page([CARD]), CONTEXT, {}, CONTEXT]
        mock_client.count = 6
        output = self.root / "out.csv"
        args = ["run", "--zones", str(zones_path), "--contract", str(contract_path), "--state", str(self.root / "run.sqlite"), "--out", str(output)]
        with patch.object(f, "Client", return_value=mock_client), patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(f.main(args), 0)
        with output.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r["h3_index"] for r in rows}), 2)
        self.assertEqual(sorted(json.loads(r["extra"])["offer_reused"] for r in rows), [False, True])
        self.assertTrue(f.load_json(output.with_suffix(".report.json"))["complete"])
        with patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(f.main(["validate", "--zones", str(zones_path), "--csv", str(output)]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
