import copy
import csv
import json
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

from samokat.catalog import Catalog, is_non_food_product
from samokat.core import Blocked, ResultStore, ROOT, load_zones, normalize_product
from samokat.captures import request_key
from test_parser import FakeAPI, PRODUCT


class SpeedTests(unittest.TestCase):
    def feed(self, bad=False):
        products = [PRODUCT]
        if bad:
            products.append({**PRODUCT, 'uuid': 'bad', 'name': 'Напиток 0,5', 'specification': ''})
        return [[{'showcaseId': 's', 'storeId': 'store'}], {'showcaseId': 's'}, {},
                [{'id': 'a', 'name': 'Вода и напитки'}],
                {'uuid': 'a', 'products': products, 'pageInfo': {'hasNextPage': False}}]

    def test_default_does_not_fetch_a_detail_for_a_missing_amount(self):
        api = FakeAPI(self.feed(bad=True))
        catalog = Catalog(api)
        rows = catalog.get_catalog('Адрес', 55.7, 37.6)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(catalog.issues), 1)
        self.assertEqual(len(api.calls), 5)
        self.assertFalse(any('/products/' in call[0] for call in api.calls))

    def test_explicit_amount_lookup_can_enrich_a_card(self):
        responses = self.feed(bad=True)
        responses.append({**PRODUCT, 'uuid': 'bad', 'name': 'Напиток', 'specification': '500 мл'})
        api = FakeAPI(responses)
        catalog = Catalog(api, resolve_amounts=True)
        rows = catalog.get_catalog('Адрес', 55.7, 37.6)
        self.assertEqual(len(rows), 2)
        self.assertEqual(catalog.issues, [])
        self.assertEqual(len(api.calls), 6)

    def test_checkpoint_throttle_keeps_final_result_and_paths(self):
        responses = [[{'id': 'a', 'name': 'Вода и напитки'},
                      {'id': 'b', 'name': 'Бакалея'}, {'id': 'c', 'name': 'Сладкое'}]]
        for cid in ('a', 'b', 'c'):
            responses.append({'uuid': cid, 'products': [PRODUCT], 'pageInfo': {'hasNextPage': False}})
        def collect(interval):
            checkpoints = []
            with patch('samokat.catalog.time.monotonic', return_value=1):
                found = Catalog(FakeAPI(copy.deepcopy(responses)), checkpoint_interval=interval).products(
                    's', on_progress=lambda value: checkpoints.append(copy.deepcopy(value)))
            return found, checkpoints
        fast, checkpoints = collect(30)
        legacy, every_category = collect(0)
        self.assertEqual(fast, legacy)
        self.assertEqual(len(checkpoints), 2)
        self.assertEqual(len(every_category), 3)
        self.assertEqual(checkpoints[-1], fast)

    def test_error_forces_latest_partial_after_suppressed_checkpoints(self):
        responses = [[{'id': 'a', 'name': 'Вода и напитки'},
                      {'id': 'b', 'name': 'Бакалея'}, {'id': 'c', 'name': 'Сладкое'}],
                     {'uuid': 'a', 'products': [PRODUCT], 'pageInfo': {'hasNextPage': False}},
                     {'uuid': 'b', 'products': [{**PRODUCT, 'uuid': 'second'}], 'pageInfo': {'hasNextPage': False}},
                     Blocked('403')]
        checkpoints = []
        with patch('samokat.catalog.time.monotonic', return_value=1), self.assertRaises(Blocked):
            Catalog(FakeAPI(responses), checkpoint_interval=30).products(
                's', on_progress=lambda value: checkpoints.append(copy.deepcopy(value)))
        self.assertEqual(set(checkpoints[-1]), {'p1', 'second'})

    def test_extra_status_optimization_does_not_modify_product(self):
        zone = load_zones(ROOT / 'data/hexagons.csv')[0]
        product = normalize_product(PRODUCT, 'Молоко', {})
        original = copy.deepcopy(product)
        with tempfile.TemporaryDirectory() as directory:
            store = ResultStore(Path(directory) / 'result.csv', {})
            row = store.marked_row(product, zone, 'partial', 2)
            store.close()
        extra = json.loads(row['extra'])
        self.assertFalse(extra['zone_complete'])
        self.assertEqual(extra['quarantined_products'], 2)
        self.assertEqual(product, original)

    def test_observed_nonfood_products_are_excluded_but_spices_are_kept(self):
        for name in ('Букет Комплимент №45, сухоцветы', 'Колпачок для трубочки Котёнок',
                     'Бумажные формы для выпечки', 'Чайная пара с сердечками'):
            self.assertTrue(is_non_food_product({'name': name}), name)
        for name in ('Букет гарни, 10 г', 'Чай зелёный, 20 пакетиков', 'Набор специй Kotanyi'):
            self.assertFalse(is_non_food_product({'name': name}), name)

    def test_explicit_output_routes_csv_checkpoint_and_resume_to_requested_path(self):
        from samokat.__main__ import main
        zone = load_zones(ROOT / 'data/hexagons.csv')[0]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            captures = directory / 'responses'
            captures.mkdir()
            request = json.loads(request_key('/showcases/list', params={
                'lat': float(zone['address_lat']), 'lon': float(zone['address_lon'])}))
            (captures / 'one.json').write_text(json.dumps({'request': request, 'data': [],
                                                         'captured_at': '2026-10-21 17:00:00'}), encoding='utf-8')
            output = directory / 'requested' / 'samokat_2026-10-21_arkhipov_recovered.csv'
            argv = ['samokat', 'recover', '--limit', '1', '--output', str(output), '--capture-dir', str(captures)]
            with patch.object(sys, 'argv', argv), patch('samokat.__main__.setup_logging'), self.assertLogs('samokat', level='INFO') as logs:
                self.assertEqual(main(), 0)
            self.assertTrue(any(str(output.resolve()) in line for line in logs.output))
            self.assertTrue(output.with_suffix('.sqlite3').is_file())
            self.assertTrue(output.with_suffix('.status.json').is_file())
            self.assertEqual({path.name for path in output.parent.glob('*.csv')},
                             {output.name, output.with_suffix('.partial.csv').name})
            saved = output.read_bytes()
            with patch.object(sys, 'argv', argv), patch('samokat.__main__.setup_logging'):
                self.assertEqual(main(), 0)
            self.assertEqual(output.read_bytes(), saved)

    def test_cached_driver_versions_are_sorted_numerically(self):
        from samokat.firefox import cached_geckodriver
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            for version in ('0.9.1', '0.10.0', 'not-a-version'):
                path = directory / 'geckodriver/win64' / version / 'geckodriver.exe'
                path.parent.mkdir(parents=True)
                path.write_bytes(b'test fixture, never executed')
            with patch('samokat.firefox.os.name', 'nt'):
                self.assertEqual(Path(cached_geckodriver(directory)).parent.name, '0.10.0')

    def test_empty_driver_cache_falls_back_to_manager(self):
        from samokat.firefox import cached_geckodriver
        with tempfile.TemporaryDirectory() as directory, patch('samokat.firefox.os.name', 'nt'):
            self.assertIsNone(cached_geckodriver(directory))

    def test_driver_refresh_flag_disables_cached_lookup(self):
        from types import SimpleNamespace
        from samokat.__main__ import browser
        args = SimpleNamespace(firefox=None, geckodriver=None, interval=10, startup_timeout=60,
                               headless=False, capture_dir=None, refresh_driver=True)
        with patch('samokat.firefox.FirefoxAPI') as factory:
            browser(args)
        self.assertFalse(factory.call_args.kwargs['use_cached_driver'])


if __name__ == '__main__':
    unittest.main()
