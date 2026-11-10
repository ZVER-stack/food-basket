"""Offline checks for isolation, checkpoint aggregation and pool stop behavior."""
import argparse
from contextlib import nullcontext
import csv
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import ozon_fresh_fast as core
import ozon_parallel as parallel
import ozon_browser_transport as transport
from ozon_run_lock import browser_lock_path

ROOT=Path(__file__).resolve().parent

class ParallelChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def state(self,index):
        folder=self.root/f'worker_{index}';folder.mkdir()
        state=core.State(folder/'snapshot.sqlite','fixture')
        self.addCleanup(state.db.close)
        return state

    def row(self,sku='123'):
        return dict(product_id=sku,name='Молоко 1 л',price='100',price_regular='110',amount='1 л')

    def test_all_houses_partition_once_and_roundtrip_house_coordinates(self):
        zones,skipped=core.load_zones(ROOT/'hexagons.csv')
        parts=parallel.partition_zones(zones,4)
        self.assertEqual((len(zones),len(skipped)),(197,4))
        self.assertEqual(sorted(map(len,parts)),[49,49,49,50])
        path=self.root/'zones.csv';parallel.write_zone_file(path,parts[0])
        self.assertEqual(core.load_zones(path),(parts[0],[]))
        self.assertFalse({z['h3_index'] for z in skipped}&{z['h3_index'] for part in parts for z in part})

    def test_committed_rows_survive_missing_worker_csv_after_crash(self):
        self.state(1).mark('zone-a','complete',[self.row()])
        self.state(2).mark('zone-b','no_delivery')
        report=parallel.aggregate(self.root,{'zone-a','zone-b'})
        self.assertTrue(report['complete'])
        self.assertEqual((report['rows'],report['completed_zones']),(2,2))
        with (self.root/'ozon_fresh.csv').open(encoding='utf-8',newline='') as stream:
            reader=csv.DictReader(stream);rows=list(reader)
            self.assertEqual(reader.fieldnames,core.COLUMNS)
        self.assertEqual(rows[0]['price'],'100')
        self.assertEqual(rows[1]['product_id'],'')

    def test_incomplete_page_checkpoints_are_not_exported_as_finished_zones(self):
        self.state(1).put_page('zone-a','https://www.ozon.ru/category/x/',core.now(),[self.row()],None)
        report=parallel.aggregate(self.root,{'zone-a'})
        self.assertFalse(report['complete']);self.assertEqual(report['rows'],0)

    def test_duplicate_zone_or_missing_price_does_not_replace_good_export(self):
        target=self.root/'ozon_fresh.csv';target.write_text('previous',encoding='utf-8')
        state=self.state(1);state.mark('zone-a','complete',[dict(self.row(),price='')])
        with self.assertRaises(core.ParseError):parallel.aggregate(self.root,{'zone-a'})
        self.assertEqual(target.read_text(encoding='utf-8'),'previous')
        state.mark('zone-a','complete',[self.row()])
        self.state(2).mark('zone-a','no_delivery')
        with self.assertRaises(core.ParseError):parallel.aggregate(self.root,{'zone-a'})

    def test_blocked_preflight_creates_no_worker_or_snapshot_clock(self):
        args=argparse.Namespace(zones=str(ROOT/'hexagons.csv'),categories=str(ROOT/'categories_live.json'),
            limit=2,workers=2,out_dir=str(self.root),interval=1.05,seconds=7200,cdp='http://127.0.0.1:9222')
        with patch.object(parallel,'profile_lock',return_value=nullcontext()),patch.object(parallel,'preflight',side_effect=core.ProtectionError('HTTP403')),patch.object(parallel.mp,'get_context') as context:
            with self.assertRaises(core.ProtectionError):parallel.run(args)
        context.return_value.Process.assert_not_called()
        self.assertFalse((self.root/'parallel_manifest.json').exists())

    def test_worker_failure_sets_stop_signal(self):
        event=threading.Event()
        with patch.object(parallel.live,'run',return_value=2):
            parallel.worker_run({'out_dir':str(self.root)},event)
        self.assertTrue(event.is_set())

    def test_other_worker_protection_prevents_another_site_request(self):
        client=transport.BrowserTransport.__new__(transport.BrowserTransport)
        client.stop_event=threading.Event();client.stop_event.set();client.page=Mock()
        with self.assertRaises(core.BudgetError):client.entrypoint('/category/supermarket-25000/')
        client.page.evaluate.assert_not_called()

    def test_api_product_mode_uses_one_public_api_and_no_navigation(self):
        client=transport.BrowserTransport.__new__(transport.BrowserTransport)
        client.check=Mock();client.entrypoint=Mock(return_value={'fixture':True});client.page=Mock();client.product_mode='api'
        self.assertEqual(client.product_page('/product/milk-123/?miniapp=supermarket'),{'fixture':True})
        client.entrypoint.assert_called_once_with('/product/milk-123/?miniapp=supermarket')
        client.page.goto.assert_not_called()

    def test_independent_context_never_copies_default_profile_cookies(self):
        runtime=Mock();browser=runtime.chromium.connect_over_cdp.return_value
        owned=browser.new_context.return_value;page=owned.new_page.return_value
        page.url='https://www.ozon.ru/category/supermarket-25000/'
        page.goto.return_value.status=200;page.title.return_value='Ozon Fresh'
        page.content.return_value='<div id="state-profileMenuAnonymous-x" data-state="{}"></div>'
        with patch.object(transport,'sync_playwright') as factory:
            factory.return_value.start.return_value=runtime
            client=transport.BrowserTransport(state_path=self.root/'transport.json',isolated_context=True)
            self.assertEqual(client.count,1)
            client.close()
        browser.new_context.assert_called_once()
        browser.cookies.assert_not_called();owned.add_cookies.assert_not_called()
        owned.close.assert_called_once();browser.close.assert_not_called()

    def test_lock_normalizes_localhost_and_isolates_worker_contexts(self):
        self.assertEqual(browser_lock_path('http://localhost:9222'),browser_lock_path('http://127.0.0.1:9222'))
        self.assertNotEqual(browser_lock_path('http://localhost:9222',1),browser_lock_path('http://localhost:9222',2))

if __name__=='__main__':unittest.main()
