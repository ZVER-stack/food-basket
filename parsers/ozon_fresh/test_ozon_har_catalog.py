"""Regression tests against three actual anonymous Ozon tile fixtures."""
import copy
import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import ozon_fresh_fast as core
import ozon_har_catalog as o

FIXTURE = core.load_json(Path(__file__).parent / "testdata" / "public_tiles.json")


class RealOzonTileTests(unittest.TestCase):
    def test_gram_abbreviation_from_real_failed_product_is_preserved(self):
        name='Низкокалорийный соус Mr.Djemius ZERO "Сырный" 330гр'
        self.assertEqual(core.AMOUNT.findall(name),['330гр'])
        self.assertEqual(o.full_amount('330гр'),'330гр')

    def test_pack_factor_in_title_is_preserved_without_weight_calculation(self):
        self.assertEqual(core.AMOUNT.findall('Йогурт 6×100 г'),['6×100 г'])
        self.assertEqual(core.AMOUNT.findall('Сок 4 шт. × 200 мл'),['4 шт. × 200 мл'])

    def test_real_stick_count_product_has_literal_amount(self):
        fixture=core.load_json(Path(__file__).parent/'testdata/count_unit_tile.json')
        self.assertFalse(fixture['synthetic'])
        row=o.tile_product(fixture['card'])
        self.assertEqual(row['product_id'],'3927006263')
        self.assertEqual(row['amount'],'20 стиков')
        self.assertEqual(row['price'],'587')

    def test_observed_separator_tail_requires_matching_sold_out_continuation(self):
        url='/category/supermarket-ovoshchi-frukty-zelen-9201000/?page=32&layout_page_index=32&sold_out_page=1&miniapp=supermarket'
        data={'pageInfo':{'url':url,'pageType':'category'},'layout':[{'component':'separator','stateId':'separator-3669747-default-32',
            'widgetTrackingInfo':'{"name":"cms.separator"}'}],'widgetStates':{'separator-3669747-default-32':{'height':12}}}
        self.assertTrue(o.empty_catalog_tail(data,url))
        self.assertFalse(o.empty_catalog_tail(data,url.replace('page=32','page=1')))
        data['layout'][0]['component']='recommendations'
        self.assertFalse(o.empty_catalog_tail(data,url))

    def test_recommendation_grid_is_excluded_even_when_it_has_same_component(self):
        data={'layout':[{'component':'tileGridDesktop','stateId':'tileGridDesktop-search',
            'widgetTrackingInfo':{'name':'catalog.searchResultsV2'}},
            {'component':'tileGridDesktop','stateId':'tileGridDesktop-shelf','widgetTrackingInfo':{'name':'shelf'}}],
            'widgetStates':{'tileGridDesktop-search':{'items':[{'sku':'123'}]},'tileGridDesktop-shelf':{'items':[{'sku':'999'}]}},'nextPage':None}
        self.assertEqual(o.primary_page(data)['items'],[{'sku':'123'}])
        data['layout']=data['layout'][1:]
        with self.assertRaises(core.ParseError):o.primary_page(data)

    def test_actual_weighted_tile_keeps_unit_price_separate(self):
        fixture=core.load_json(Path(__file__).parent/'testdata/unit_price_tile.json')
        self.assertFalse(fixture['synthetic'])
        row=o.tile_product(fixture['card'])
        self.assertIsNone(row['price'])
        self.assertEqual((row['price_card'],row['price_regular']),('149','179'))
        self.assertEqual((row['unit_price'],row['unit_price_basis']),('149','1кг'))
        self.assertEqual(row['extra']['unit_price_kind'],'card')

    def test_actual_weight_range_is_not_reduced_to_its_upper_bound(self):
        fixture=core.load_json(Path(__file__).parent/'testdata/unit_price_tile.json')
        self.assertEqual(o.tile_product(fixture['card'])['amount'],'1-1.50 кг')

    def test_fixtures_are_real_captured_tiles(self):
        self.assertFalse(FIXTURE["synthetic"])
        self.assertEqual(set(FIXTURE["samples"]), {"ACTUAL_PRICE", "SALE_PRICE", "CARD_PRICE"})

    def test_actual_and_sale_styles_have_ordinary_prices(self):
        for style in ("ACTUAL_PRICE", "SALE_PRICE"):
            with self.subTest(style=style):
                row = o.tile_product(FIXTURE["samples"][style])
                self.assertIsNotNone(row["price"])
                self.assertIsNotNone(row["price_regular"])
                self.assertIsNone(row["price_card"])
                self.assertTrue(row["amount"])

    def test_card_style_does_not_invent_ordinary_price(self):
        row = o.tile_product(FIXTURE["samples"]["CARD_PRICE"])
        self.assertIsNone(row["price"])
        self.assertIsNotNone(row["price_card"])

    def test_atom_and_price_order_do_not_change_meanings(self):
        for style, original in FIXTURE["samples"].items():
            card = copy.deepcopy(original)
            card["mainState"].reverse()
            for atom in card["mainState"]:
                if atom["type"] == "priceV2":
                    atom["priceV2"]["price"].reverse()
            self.assertEqual(o.tile_product(card), o.tile_product(original))

    def test_marketing_words_cannot_become_product_name(self):
        card = copy.deepcopy(FIXTURE["samples"]["ACTUAL_PRICE"])
        card["mainState"].insert(0, {"type": "textDS", "textDS": {"text": "Распродажа 99%"}})
        self.assertEqual(o.tile_product(card)["name"], o.tile_product(FIXTURE["samples"]["ACTUAL_PRICE"])["name"])

    def test_without_fresh_action_product_is_rejected(self):
        card = copy.deepcopy(FIXTURE["samples"]["ACTUAL_PRICE"])
        del card["multiButton"]
        with self.assertRaises(core.ParseError):
            o.tile_product(card)

    def test_fresh_button_must_match_product_sku(self):
        card = copy.deepcopy(FIXTURE["samples"]["ACTUAL_PRICE"])
        card["multiButton"]["expressButton"]["addToCart"]["actionButton"]["common"]["action"]["params"]["id"] = "999999"
        with self.assertRaises(core.ParseError):
            o.tile_product(card)

    def test_delivery_schema_is_not_exported_as_warehouse_or_stock(self):
        row = o.tile_product(FIXTURE["samples"]["ACTUAL_PRICE"])
        self.assertNotIn("store_id", row)
        self.assertNotIn("stock_qty", row)
        self.assertEqual(row["extra"]["delivery_schema"], "131")

    def test_tracking_parameters_removed_from_product_url(self):
        card = copy.deepcopy(FIXTURE["samples"]["ACTUAL_PRICE"])
        card["action"]["link"] += "?at=private-tracking&miniapp=supermarket"
        self.assertNotIn("?", o.tile_product(card)["url"])

    def test_quantity_does_not_use_rating_or_percentage(self):
        self.assertIsNone(o.full_amount("4.9"))
        self.assertIsNone(o.full_amount("82.5%"))
        self.assertEqual(o.full_amount("6 шт × 100 г"), "6 шт × 100 г")

    def test_html_embedded_states_accept_reversed_attribute_order(self):
        state = {"customCell": {"actionMode": "common", "cells": [{"text": {"text": "Бакунинская ул., 60/2"}}]}}
        import html
        text = '<div data-state="' + html.escape(json.dumps(state), quote=True) + '" id="state-addressBookBarWeb-fixture"></div>'
        self.assertEqual(o.address_title(o.document_states(text)), "Бакунинская ул., 60/2")
        self.assertEqual(o.normalized_address("Москва, Бакунинская улица, 60/2"), o.normalized_address(o.address_title(o.document_states(text))))

    def test_actual_root_and_first_page_pagination_formats(self):
        grid = {"items": list(FIXTURE["samples"].values()), "page": 1}
        data = {"widgetStates": {"tileGridDesktop-dynamic-1": json.dumps(grid), "infiniteVirtualPaginator-dynamic-1": json.dumps({"nextPage": "/category/food/?page=2"})}}
        self.assertFalse(o.primary_page(data)["terminal"])
        data["nextPage"] = None
        self.assertTrue(o.primary_page(data)["terminal"])
        schema = {"widget_prefix": "tileGridDesktop-", "items": "/items", "next": "/nextPage", "pagination_widget_prefix": "infiniteVirtualPaginator-"}
        del data["nextPage"]
        self.assertEqual(core.catalog_page(data, schema)[1], "/category/food/?page=2")

    def test_missing_pagination_is_not_assumed_terminal(self):
        with self.assertRaises(core.ParseError):
            o.primary_page({"widgetStates": {"tileGridDesktop-x": {"items": []}}})

    def test_capture_timestamp_converted_to_moscow(self):
        self.assertEqual(o.parse_timestamp({"startedDateTime": "2026-10-17T11:25:53Z"}), "2026-10-17 14:25:53")
        with self.assertRaises(core.ParseError):
            o.parse_timestamp({"startedDateTime": "2026-10-17T11:25:53"})

    def test_unknown_house_cannot_become_zone_by_guessing(self):
        self.assertEqual(o.address_title({"widgetStates": {"addressBookBarWeb-x": {"customCell": {"actionMode": "individual", "cells": []}}}}), "")

    def test_categories_follow_food_ancestry_and_refine_household_root(self):
        parent = {"title": "Бакалея", "url": "/category/bakaleya-10/?miniapp=supermarket", "childs": [{"title": "Крупы", "url": "/category/krupy-11/?miniapp=supermarket"}]}
        menu = {"items": [
            {"title": "ПРОДУКТЫ", "columns": [{"items": [parent]}]},
            {"title": "ЭЛЕКТРОНИКА", "columns": [{"items": [{"title": "Рисоварки", "url": "/category/risovarki-12/?miniapp=supermarket"}]}]},
            {"title": "ДОМ И УЮТ", "columns": [{"items": [{"title": "Бытовая химия и хозтовары", "url": "/category/hoztovary-13/?miniapp=supermarket", "childs": []}]}]}
        ]}
        food, household = o.captured_categories(menu)
        self.assertEqual([c["category_id"] for c in food], ["11"])
        self.assertTrue(household[0]["requires_refinement"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
