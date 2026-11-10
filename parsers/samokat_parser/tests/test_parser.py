import copy
import csv
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from samokat.catalog import Catalog, category_paths, choose_showcase
from samokat.core import (COLUMNS, ROOT, Blocked, ParserError, ResultStore, SchemaError,
                          amount_from_product, load_zones, normalize_product, rubles, to_row)


ZONE = load_zones(ROOT / 'data/hexagons.csv')[0]
PRODUCT = {'uuid': 'p1', 'name': 'Молоко, 3,2%', 'specification': '950 мл',
           'prices': {'current': 8990, 'old': 10900}, 'quantity': 10,
           'attributes': {'kilocalories': '58', 'protein': '3,2', 'fats': '3.2', 'carbohydrates': '4.7'},
           'badge': {'text': 'Акция'}, 'promoMarkup': {'badge': {'text': 'Акция'}},
           'detailsAttributes': [{'displayName': 'Состав', 'value': 'молоко, сахар', 'type': 'STRING'}],
           'unrecognizedField': {'barcode': '4601234567890'}}


class CoreTests(unittest.TestCase):
    def test_all_201_zones_and_exact_columns(self):
        self.assertEqual(len(load_zones(ROOT / 'data/hexagons.csv')), 201)
        self.assertEqual(len(COLUMNS), 37)

    def test_money_decimal_no_rounding_or_scaling_error(self):
        self.assertEqual(rubles(8990), '89.9')
        self.assertEqual(rubles('100000'), '1000')
        self.assertEqual(rubles(0), '0')
        self.assertEqual(rubles(None), '')

    def test_normalization_and_unknown_fields_preserved(self):
        product = normalize_product(PRODUCT, 'Молочное > Молоко', {})
        self.assertEqual(product['price'], '89.9')
        self.assertEqual(product['price_regular'], '109')
        self.assertEqual(product['protein'], '3.2')
        self.assertEqual(product['composition'], 'молоко, сахар')
        self.assertEqual(product['labels'], 'Акция')
        self.assertNotIn('stock_qty', product)
        row = to_row({**product, 'new_field': 'test', 'service': 'lavka'}, ZONE)
        extra = json.loads(row['extra'])
        self.assertEqual(extra['new_field'], 'test')
        self.assertEqual(extra['raw_product']['unrecognizedField']['barcode'], '4601234567890')
        self.assertEqual(row['service'], 'samokat')
        self.assertEqual(list(row), COLUMNS)

    def test_no_discount_regular_equals_current(self):
        product = copy.deepcopy(PRODUCT)
        product['prices'] = {'current': 8900}
        row = normalize_product(product, '', {})
        self.assertEqual(row['price_regular'], '89')

    def test_amount_from_realistic_name_without_specification(self):
        for name, amount in [('Чёрный чай Kioko, Масала, 25 шт.', '25 шт.'),
                             ('Подарочный набор, саше 30x2 г', '30x2 г'),
                             ('Йогурт, 6 шт × 100 г', '6 шт × 100 г'),
                             ('Кутабы с сыром, 3 шт.,150 г', '3 шт.,150 г'),
                             ('Молоко, 3,2%, 950 мл', '950 мл')]:
            self.assertEqual(amount_from_product({'name': name})[0], amount)
        self.assertEqual(amount_from_product({'name': 'Брелок Сердце'})[0], '')

    def test_real_public_product_schema_captured_in_firefox(self):
        raw = json.loads((Path(__file__).parent / 'fixtures/tea_2026-10-21.json').read_text(encoding='utf-8'))
        row = normalize_product(raw, 'Бакалея > Чай', {})
        self.assertEqual((row['price'], row['price_regular']), ('179', '259'))
        self.assertEqual(row['amount'], '25 шт.')
        self.assertEqual(row['brand'], 'Kioko')
        self.assertEqual(row['country'], 'Россия')
        self.assertTrue(row['composition'].startswith('Чай чёрный индийский'))

    def test_missing_required_and_invalid_numbers_rejected(self):
        for field, value in [('specification', ''), ('uuid', ''), ('name', '')]:
            with self.subTest(field=field), self.assertRaises(SchemaError):
                normalize_product({**PRODUCT, field: value}, '', {})
        for value in ('NaN', 'Infinity', -1, True, None):
            product = {**PRODUCT, 'prices': {'current': value}}
            with self.subTest(value=value), self.assertRaises(SchemaError):
                normalize_product(product, '', {})

    def test_csv_roundtrip_checkpoint_and_idempotent_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'samokat.csv'
            product = normalize_product(PRODUCT, 'Молоко', {})
            product['description'] = 'Строка 1\n"Строка 2", состав'
            store = ResultStore(target, {'test': 1})
            store.save_zone(ZONE, [product])
            store.close()
            store = ResultStore(target, {'test': 1})
            self.assertTrue(store.done(ZONE['h3_index']))
            store.save_zone(ZONE, [product])
            store.close()
            with target.open(encoding='utf-8', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['description'], product['description'])
            self.assertEqual(rows[0]['amount'], '950 мл')
            with self.assertRaises(ParserError):
                ResultStore(target, {'test': 2})

    def test_empty_catalog_not_saved_as_no_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResultStore(Path(directory) / 'data.csv', {})
            with self.assertRaises(SchemaError):
                store.save_zone(ZONE, [])
            self.assertFalse(store.done(ZONE['h3_index']))
            store.save_zone(ZONE, [], no_delivery=True)
            with store.path.open(encoding='utf-8', newline='') as f:
                row = next(csv.DictReader(f))
            self.assertEqual([k for k, v in row.items() if v], ['service', 'h3_index', 'parsed_at'])
            store.close()

    def test_export_failure_recovers_from_committed_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'data.csv'
            store = ResultStore(target, {})
            with patch.object(store, 'export', side_effect=PermissionError('CSV открыт в Excel')):
                with self.assertRaises(PermissionError):
                    store.save_zone(ZONE, [normalize_product(PRODUCT, '', {})])
            store.close()
            recovered = ResultStore(target, {})
            self.assertTrue(recovered.done(ZONE['h3_index']))
            with target.open(encoding='utf-8', newline='') as f:
                self.assertEqual(len(list(csv.DictReader(f))), 1)
            recovered.close()


class FakeAPI:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def query(self, url, method='GET', body=None, params=None):
        self.calls.append((url, method, body, params))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)


class CatalogTests(unittest.TestCase):
    def test_showcase_empty_only_means_no_delivery(self):
        self.assertIsNone(choose_showcase([]))
        for bad in (None, {}, {'data': []}, [{}]):
            with self.subTest(bad=bad), self.assertRaises(SchemaError):
                choose_showcase(bad)
        choices = [{'showcaseId': 'super', 'type': 'SUPERMARKET'}, {'showcaseId': 'mini', 'type': 'MINIMARKET'}]
        self.assertEqual(choose_showcase(choices)['showcaseId'], 'mini')

    def test_paths_and_cycles(self):
        nodes = [{'id': 'a', 'name': 'Молочное'}, {'id': 'b', 'parentId': 'a', 'name': 'Молоко'}]
        self.assertEqual(category_paths(nodes)['b'], 'Молочное > Молоко')
        with self.assertRaises(SchemaError):
            category_paths([{'id': 'a', 'parentId': 'a', 'name': 'Цикл'}])

    def test_repeated_categories_and_multiple_parents(self):
        a = {'id': 'a', 'name': 'Молочное'}
        b = {'id': 'b', 'name': 'Акции'}
        milk1 = {'id': 'm', 'parentId': 'a', 'name': 'Молоко'}
        milk2 = {'id': 'm', 'parentId': 'b', 'name': 'Молоко'}
        paths = category_paths([a, a, b, milk1, milk2], all_paths=True)
        self.assertEqual(paths['m'], ['Молочное > Молоко', 'Акции > Молоко'])

    def test_pagination_uses_raw_offset(self):
        p2 = {**PRODUCT, 'uuid': 'p2'}
        api = FakeAPI([{'items': [PRODUCT, p2], 'pageInfo': {'hasNextPage': True}},
                       {'items': [p2], 'pageInfo': {'hasNextPage': False}}])
        pages = list(Catalog(api).pages('s1', 'c1'))
        self.assertEqual(sum(map(len, pages)), 3)
        self.assertEqual(api.calls[1][2]['offset'], 2)

    def test_repeated_page_empty_page_and_missing_end_marker(self):
        first = {'items': [PRODUCT], 'pageInfo': {'hasNextPage': True}}
        for other in (first, {'items': [], 'pageInfo': {'hasNextPage': True}}, {'items': [PRODUCT]}):
            with self.subTest(other=other), self.assertRaises(SchemaError):
                list(Catalog(FakeAPI([first, other])).pages('s1', 'c1'))

    def test_same_product_multiple_categories_one_row(self):
        categories = [{'id': 'a', 'name': 'Молоко, яйца и сыр'}, {'id': 'b', 'parentId': 'a', 'name': 'Молоко'}]
        tree = {'uuid': 'a', 'products': [PRODUCT], 'pageInfo': {'hasNextPage': False}, 'categories': [
            {'uuid': 'b', 'name': 'Молоко', 'products': [PRODUCT], 'pageInfo': {'hasNextPage': False}}]}
        found = Catalog(FakeAPI([categories, tree])).products('s1')
        self.assertEqual(len(found), 1)
        self.assertEqual(found['p1']['paths'], ['Молоко, яйца и сыр', 'Молоко, яйца и сыр > Молоко'])

    def test_food_scope_excludes_promos_with_non_food(self):
        from samokat.catalog import food_categories
        categories = [{'id': 'a', 'name': 'Молоко, яйца и сыр'},
                      {'id': 'promo', 'name': 'Новинки'}, {'id': 'other', 'name': 'Красота и здоровье'},
                      {'id': 'kids', 'name': 'Для детей'},
                      {'id': 'baby', 'name': 'Детское питание', 'parentId': 'kids'},
                      {'id': 'toys', 'name': 'Игрушки', 'parentId': 'kids'}]
        self.assertEqual(set(food_categories(categories)), {'a', 'baby'})

    def test_baking_accessories_within_food_section_are_excluded(self):
        categories = [{'id': 'a', 'name': 'Мясо и рыба'}]
        foil = {**PRODUCT, 'uuid': 'foil', 'name': 'Алюминиевая фольга, 10 м'}
        tree = {'uuid': 'a', 'products': [PRODUCT, foil], 'pageInfo': {'hasNextPage': False}, 'categories': [
            {'uuid': 'b', 'name': 'Пакеты для запекания', 'pageInfo': {'hasNextPage': False}}]}
        found = Catalog(FakeAPI([categories, tree])).products('s1')
        self.assertEqual(set(found), {'p1'})

    def test_bulk_tree_continues_truncated_child_from_correct_offset(self):
        categories = [{'id': 'a', 'name': 'Молоко, яйца и сыр'}]
        tree = {'uuid': 'a', 'products': [], 'pageInfo': {'hasNextPage': False}, 'categories': [
            {'uuid': 'b', 'name': 'Молоко', 'products': [PRODUCT], 'pageInfo': {'hasNextPage': True}}]}
        api = FakeAPI([categories, tree, {'items': [{**PRODUCT, 'uuid': 'p2'}], 'pageInfo': {'hasNextPage': False}}])
        found = Catalog(api).products('s1')
        self.assertEqual(set(found), {'p1', 'p2'})
        self.assertEqual(api.calls[-1][2]['offset'], 1)

    def test_navigation_tile_is_loaded_even_when_has_next_is_false(self):
        categories = [{'id': 'a', 'name': 'Молоко, яйца и сыр'},
                      {'id': 'b', 'name': 'Молоко', 'parentId': 'a', 'productsCount': 1}]
        tree = {'uuid': 'a', 'pageInfo': {'hasNextPage': False}, 'categories': [
            {'uuid': 'b', 'name': 'Молоко', 'pageInfo': {'hasNextPage': False}}]}
        expanded = {'uuid': 'b', 'products': [PRODUCT], 'pageInfo': {'hasNextPage': False}}
        api = FakeAPI([categories, tree, expanded])
        found = Catalog(api).products('s1')
        self.assertEqual(set(found), {'p1'})
        self.assertTrue(api.calls[-1][0].endswith('/categories/b'))

    def test_two_zones_append_one_header_and_recover_torn_csv(self):
        zones = load_zones(ROOT / 'data/hexagons.csv')[:2]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'data.csv'
            store = ResultStore(target, {})
            for zone in zones:
                store.save_zone(zone, [normalize_product(PRODUCT, '', {})])
            store.close()
            with target.open('ab') as f:
                f.write(b'partially written row')
            store = ResultStore(target, {})
            store.close()
            with target.open(encoding='utf-8', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 2)
            self.assertEqual({r['h3_index'] for r in rows}, {z['h3_index'] for z in zones})

    def test_block_never_converted_to_empty_catalog(self):
        with self.assertRaises(Blocked):
            Catalog(FakeAPI([Blocked('HTTP 429')])).get_catalog('Москва', 55.7, 37.6)

    def test_cache_keeps_prices_but_refreshes_delivery_for_each_zone(self):
        showcase = [{'showcaseId': 's1', 'storeId': 'dark1', 'type': 'MINIMARKET'}]
        settings1 = {'showcaseId': 's1', 'orderSettings': {'minimalAmount': 10000}}
        settings2 = {'showcaseId': 's1', 'orderSettings': {'minimalAmount': 20000}}
        cats = [{'id': 'a', 'name': 'Молоко, яйца и сыр'}]
        page = {'uuid': 'a', 'products': [PRODUCT], 'pageInfo': {'hasNextPage': False}}
        api = FakeAPI([showcase, settings1, {'sla': {'value': 15, 'timeUnit': 'MINUTES'}}, cats, page,
                       showcase, settings2, {'sla': {'value': 1, 'timeUnit': 'HOURS'}}])
        catalog = Catalog(api)
        first = catalog.get_catalog('Адрес 1', 55.7, 37.6)
        second = catalog.get_catalog('Адрес 2', 55.8, 37.7)
        self.assertEqual(first[0]['min_order'], '100')
        self.assertEqual(second[0]['min_order'], '200')
        self.assertEqual(second[0]['delivery_eta_min'], '60')
        self.assertTrue(second[0]['extra']['catalog_reused'])
        self.assertEqual(second[0]['extra']['delivery_address'], 'Адрес 2')
        self.assertEqual(sum('categories/list' in c[0] for c in api.calls), 1)


class FirefoxGuardTests(unittest.TestCase):
    def make_api(self):
        from samokat.firefox import FirefoxAPI
        api = FirefoxAPI.__new__(FirefoxAPI)
        api.stopped = threading.Event()
        api.reason = ''
        api.driver = None
        api.capture_dir = None
        return api

    def test_preflight_event_without_intercept_does_not_continue_twice(self):
        from unittest.mock import Mock
        api = self.make_api()
        request = Mock()
        request._params = {'isBlocked': False}
        api._before(request)
        request.continue_request.assert_not_called()
        request.fail.assert_not_called()

    def test_navigation_without_body_is_not_a_captcha_or_exception(self):
        from unittest.mock import Mock
        api = self.make_api()
        api.driver = Mock()
        api.driver.current_url = 'https://samokat.ru/'
        api.driver.execute_script.return_value = None
        api.check()
        self.assertFalse(api.stopped.is_set())

    def test_403_stops_without_leaking_query_parameters(self):
        api = self.make_api()
        api._response({'response': {'url': 'https://api-web.samokat.ru/showcases/list?secret=test', 'status': 403}})
        with self.assertRaises(Blocked):
            api.check()
        self.assertNotIn('secret', api.reason)

    def test_visible_captcha_stops(self):
        from unittest.mock import Mock
        api = self.make_api()
        api.driver = Mock()
        api.driver.current_url = 'https://samokat.ru/'
        api.driver.execute_script.return_value = 'Подтвердите, что вы не робот'
        with self.assertRaises(Blocked):
            api.check()

    def test_temporary_network_failure_retries_then_succeeds(self):
        from unittest.mock import Mock
        api = self.make_api()
        api.driver = Mock()
        api.driver.current_url = 'https://samokat.ru/'
        api.driver.execute_script.return_value = ''
        api.driver.execute_async_script.side_effect = [
            {'error': {'status': 'FETCH_ERROR'}}, {'data': []}]
        with patch('samokat.firefox.time.sleep'):
            self.assertEqual(api.query('/showcases/list'), [])
        self.assertEqual(api.driver.execute_async_script.call_count, 2)

    def test_api_403_is_not_retried(self):
        from unittest.mock import Mock
        api = self.make_api()
        api.driver = Mock()
        api.driver.current_url = 'https://samokat.ru/'
        api.driver.execute_script.return_value = ''
        api.driver.execute_async_script.return_value = {'error': {'status': 403}}
        with self.assertRaises(Blocked):
            api.query('/showcases/list')
        self.assertEqual(api.driver.execute_async_script.call_count, 1)

    def test_temporary_network_retries_are_bounded(self):
        from unittest.mock import Mock
        api = self.make_api()
        api.driver = Mock()
        api.driver.current_url = 'https://samokat.ru/'
        api.driver.execute_script.return_value = ''
        api.driver.execute_async_script.return_value = {'error': {'status': 'FETCH_ERROR'}}
        with patch('samokat.firefox.time.sleep'), self.assertRaises(ParserError):
            api.query('/showcases/list')
        self.assertEqual(api.driver.execute_async_script.call_count, 3)


if __name__ == '__main__':
    unittest.main()
