import csv
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from samokat.captures import MissingCapture, QuerySession
from samokat.catalog import Catalog, food_categories, is_non_food_product
from samokat.core import Blocked, ResultStore, amount_from_product, load_zones, ROOT
from test_parser import FakeAPI, PRODUCT


class ResumeTests(unittest.TestCase):
    def record(self, directory, name='one.json', value=None, stamp='2026-10-21 17:05:00'):
        data = {'request': {'url': '/showcases/list', 'method': 'GET', 'timeout': 150000,
                            'params': {'lat': 55.7, 'lon': 37.6}},
                'data': value if value is not None else [], 'captured_at': stamp}
        (Path(directory) / name).write_text(json.dumps(data), encoding='utf-8')

    def test_offline_legacy_captures_preserve_time_and_never_start_browser(self):
        with tempfile.TemporaryDirectory() as d:
            self.record(d)
            factory = Mock(side_effect=AssertionError('Browser must not start'))
            with QuerySession(d, factory=factory, offline=True) as api:
                self.assertEqual(api.query('/showcases/list', params={'lon': 37.6, 'lat': 55.7}), [])
                self.assertEqual(api.last_observed_at, '2026-10-21 17:05:00')
                with self.assertRaises(MissingCapture):
                    api.query('/delivery/info')
            factory.assert_not_called()

    def test_newest_response_selected(self):
        with tempfile.TemporaryDirectory() as d:
            self.record(d, 'a.json', [1])
            self.record(d, 'b.json', [2], '2026-10-21 17:06:00')
            with QuerySession(d, offline=True) as api:
                self.assertEqual(api.query('/showcases/list', params={'lat': 55.7, 'lon': 37.6}), [2])

    def test_live_response_saved_and_reused_without_browser_on_restart(self):
        with tempfile.TemporaryDirectory() as d:
            live = Mock()
            live.query.return_value = {'items': [1]}
            with QuerySession(d, factory=lambda: live) as api:
                self.assertEqual(api.query('/delivery/info'), {'items': [1]})
                self.assertEqual(api.query('/delivery/info'), {'items': [1]})
                self.assertEqual(api.network_queries, 1)
            live.query.assert_called_once()
            with QuerySession(d, factory=Mock(side_effect=AssertionError()), offline=True) as api:
                self.assertEqual(api.query('/delivery/info'), {'items': [1]})

    def test_fresh_requests_do_not_replay_old_files(self):
        with tempfile.TemporaryDirectory() as d:
            self.record(d, value=['old'])
            live = Mock()
            live.query.return_value = ['new']
            with QuerySession(d, factory=lambda: live, reuse=False) as api:
                self.assertEqual(api.query('/showcases/list', params={'lat': 55.7, 'lon': 37.6}), ['new'])
                self.assertEqual(api.query('/showcases/list', params={'lat': 55.7, 'lon': 37.6}), ['new'])
            live.query.assert_called_once()

    def test_block_during_tree_saves_partial_without_marking_zone_complete(self):
        zone = load_zones(ROOT / 'data/hexagons.csv')[0]
        categories = [{'id': 'a', 'name': 'Молоко, яйца и сыр'},
                      {'id': 'b', 'name': 'Йогурт', 'parentId': 'a', 'productsCount': 1}]
        tree = {'uuid': 'a', 'products': [PRODUCT], 'pageInfo': {'hasNextPage': False},
                'categories': [{'uuid': 'b', 'name': 'Йогурт', 'pageInfo': {'hasNextPage': False}}]}
        api = FakeAPI([[{'showcaseId': 's', 'storeId': 'store'}],
                       {'showcaseId': 's', 'orderSettings': {}}, {}, categories, tree, Blocked('403')])
        with tempfile.TemporaryDirectory() as d:
            store = ResultStore(Path(d) / 'data.csv', {})
            with self.assertRaises(Blocked):
                Catalog(api).get_catalog(zone['address'], zone['address_lat'], zone['address_lon'],
                                        on_progress=lambda rows, issues: store.save_partial(zone, rows, issues))
            self.assertFalse(store.done(zone['h3_index']))
            with store.path.with_suffix('.partial.csv').open(encoding='utf-8', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertFalse(json.loads(rows[0]['extra'])['zone_complete'])
            store.close()

    def test_bad_product_is_quarantined_and_good_rows_survive(self):
        zone = load_zones(ROOT / 'data/hexagons.csv')[0]
        bad = {**PRODUCT, 'uuid': 'bad', 'name': 'Напиток, 0,5', 'specification': ''}
        api = FakeAPI([[{'showcaseId': 's', 'storeId': 'store'}],
                       {'showcaseId': 's', 'orderSettings': {}}, {},
                       [{'id': 'a', 'name': 'Вода и напитки'}],
                       {'uuid': 'a', 'products': [PRODUCT, bad], 'pageInfo': {'hasNextPage': False}}])
        api.offline = True
        catalog = Catalog(api)
        rows = catalog.get_catalog(zone['address'], zone['address_lat'], zone['address_lon'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(catalog.issues), 1)
        with tempfile.TemporaryDirectory() as d:
            store = ResultStore(Path(d) / 'data.csv', {})
            store.save_zone(zone, rows, issues=catalog.issues)
            self.assertTrue(store.done(zone['h3_index']))
            self.assertEqual(store.summary(), {'with_issues': 1})
            issue = json.loads(store.path.with_suffix('.issues.jsonl').read_text(encoding='utf-8'))
            self.assertEqual(issue['raw_product']['name'], 'Напиток, 0,5')
            store.close()

    def test_packet_spelling_and_explicit_detail_units(self):
        self.assertEqual(amount_from_product({'name': 'Чай, 60 пакетиков'})[0], '60 пакетиков')
        self.assertEqual(amount_from_product({'name': 'Напиток, 0,5', 'attributes': {'netContentUnit': 'ML'}})[0], '')
        raw = {'name': 'Напиток', 'detailsAttributes': [{'displayName': 'Объём', 'value': '500', 'unit': 'мл'}]}
        self.assertEqual(amount_from_product(raw), ('500 мл', 'detailsAttributes'))

    def test_ice_and_baking_moulds_are_not_food(self):
        for name in ('Форма для льда Хоккейная шайба', 'Силиконовые формы для выпечки в форме фруктов',
                     'Вспениватель для молока Energy'):
            self.assertTrue(is_non_food_product({'name': name}))
        self.assertFalse(is_non_food_product({'name': 'Смесь для выпечки, 500 г'}))

    def test_promo_baby_category_does_not_duplicate_primary_section(self):
        cats = [{'id': 'kids', 'name': 'Для детей'}, {'id': 'baby', 'parentId': 'kids', 'name': 'Детское питание'},
                {'id': 'promo', 'name': 'Цены ниже'}, {'id': 'pk', 'parentId': 'promo', 'name': 'Для детей'},
                {'id': 'pb', 'parentId': 'pk', 'name': 'Детское питание'}]
        self.assertEqual(set(food_categories(cats)), {'baby'})

    def test_native_ui_is_not_delayed_but_catalog_send_rate_is_preserved(self):
        from samokat.firefox import FirefoxAPI
        api = FirefoxAPI.__new__(FirefoxAPI)
        api.stopped = threading.Event()
        api.reason = ''
        api.condition = threading.Condition()
        api.waiters, api.ticket, api.pending = [], 0, 0
        api.interval = 0.08  # Локальная имитация; настоящая конфигурация требует >= 1 сек.
        api.last_request = time.monotonic()
        sent = []
        def request(name, url):
            return SimpleNamespace(_params={'isBlocked': True}, url=url,
                                   continue_request=lambda: sent.append((name, time.monotonic())), fail=lambda: None)
        background = request('background', 'https://api-web.samokat.ru/payments/types')
        catalog = request('catalog', 'https://api-web.samokat.ru/showcases/list')
        second_catalog = request('catalog2', 'https://api-web.samokat.ru/v2/showcases/s/categories/list')
        one = threading.Thread(target=api._before, args=(background,))
        two = threading.Thread(target=api._before, args=(catalog,))
        three = threading.Thread(target=api._before, args=(second_catalog,))
        one.start(); two.start(); three.start()
        one.join(2); two.join(2); three.join(2)
        self.assertFalse(one.is_alive() or two.is_alive() or three.is_alive())
        self.assertEqual(sent[0][0], 'background')
        self.assertEqual({s[0] for s in sent[1:]}, {'catalog', 'catalog2'})
        self.assertGreaterEqual(sent[2][1] - sent[1][1], 0.075)


if __name__ == '__main__':
    unittest.main()
