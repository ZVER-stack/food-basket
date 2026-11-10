"""Offline integration: address barriers, actual receipt timestamps and deadlines."""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json,tempfile,threading,time,unittest
from unittest.mock import patch,Mock

import ozon_fresh_fast as core
import ozon_refresh as refresh
from ozon_refresh_state import RefreshStore
from ozon_browser_transport import BrowserTransport
from test_ozon_refresh_model import item,baseline,CAT,ZONE


class FakeBaseline:
    identity='completed-offline-fixture';has_gaps=False
    def house(self,h3):
        row=baseline();row['h3_index']=h3
        return [row]
    def category_pages(self,h3):return {CAT['category_id']:4}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.args=SimpleNamespace(out=str(self.root),seconds=7200,interval=3,
            cdp='http://127.0.0.1:9222',readers=4,pages_per_house=4,max_product_checks=2)
        self.base=FakeBaseline()
        self.store=RefreshStore(self.root,self.base.identity,'fp',7200)
        self.addCleanup(self.store.close)

    def execute(self,confirm_error=None):
        location=Mock();location.switch.return_value={'delivery_available':True}
        if confirm_error:location.confirm.side_effect=confirm_error
        readers=Mock();readers.report.return_value={'requests':1}
        readers.request_many.side_effect=lambda specs:[{'error':None,'result':{
            'data':{'fixture':True},'captured_at':'2026-10-18 17:01:02.123456'}} for _ in specs]
        client=Mock();client.deadline=time.monotonic()+7200;client.count=6
        sample={'category':CAT,'page':1,'url':'https://www.ozon.ru/category/fixture-123/'}
        with ExitStack() as stack:
            for name,value in [('FreshLocation',Mock(return_value=location)),
                    ('SampleReaders',Mock(return_value=readers)),('BrowserTransport',Mock(return_value=client)),
                    ('choose_samples',Mock(return_value=[sample])),('verify_house',Mock())]:
                stack.enter_context(patch.object(refresh,name,value))
            stack.enter_context(patch('run_parser.preflight',return_value={'product_url':'https://www.ozon.ru/product/2952307571/'}))
            stack.enter_context(patch.object(refresh.tiles,'primary_catalog_grid',return_value={'items':[{'sku':'fixture'}]}))
            stack.enter_context(patch.object(refresh.tiles,'tile_product',return_value=item('ACTUAL_PRICE')))
            result=refresh.run_refresh(self.args,self.base,self.store,[ZONE],[CAT])
        return result,location,readers

    def test_receipt_microseconds_roundtrip_after_final_address_confirmation(self):
        result,location,readers=self.execute()
        self.assertEqual(result,0);location.confirm.assert_called_once();readers.close.assert_called_once()
        report=core.load_json(self.root/'refresh_report.json')
        self.assertEqual(report['observations_rows'],1)
        self.assertTrue(report['sampling_plan_completed']);self.assertFalse(report['complete'])
        row=next(iter(self.store.get_house('A')['observations'].values()))
        self.assertEqual(row['parsed_at'],'2026-10-18 17:01:02')
        self.assertEqual(json.loads(row['extra'])['observation_received_at'],'2026-10-18 17:01:02.123456')

    def test_wrong_final_address_keeps_observations_staged_out_of_csv(self):
        result,location,_=self.execute(core.ParseError('wrong address'))
        self.assertEqual(result,2);location.confirm.assert_called_once()
        report=core.load_json(self.root/'refresh_report.json')
        self.assertEqual(report['observations_rows'],0)
        self.assertEqual(report['houses_partial'],1)
        self.assertEqual(len(self.store.get_house('A')['observations']),1)

    def test_expired_pass_exports_unchecked_without_browser_or_deadline_extension(self):
        self.store.started_unix=time.time()-7201
        with patch('run_parser.preflight') as preflight,patch.object(refresh,'BrowserTransport') as transport:
            result=refresh.run_refresh(self.args,self.base,self.store,[ZONE],[CAT])
        self.assertEqual(result,0);preflight.assert_not_called();transport.assert_not_called()
        report=core.load_json(self.root/'refresh_report.json')
        self.assertEqual(report['unchecked_baseline_products'],1)
        self.assertEqual(report['houses_not_checked'],1)
        self.assertTrue(report['collection_budget_ended']);self.assertFalse(report['sampling_plan_completed'])

    def test_preflight_challenge_waits_without_sending_site_requests(self):
        with patch('run_parser.preflight',side_effect=core.ProtectionError('Challenge')),\
             patch.object(refresh.BackoffGate,'wait',side_effect=core.BudgetError('deadline fixture')),\
             patch.object(refresh,'BrowserTransport') as transport:
            result=refresh.run_refresh(self.args,self.base,self.store,[ZONE],[CAT])
        self.assertEqual(result,2);transport.assert_not_called()
        status=core.load_json(self.root/'run_status.json')
        self.assertTrue(status['requires_manual_access_check'])
        self.assertGreater(status['manual_required_since_unix'],0)

    def test_rate_pause_is_interruptible_by_shared_deadline(self):
        client=BrowserTransport.__new__(BrowserTransport);client.stop_event=threading.Event()
        client.stop_event.set();start=time.monotonic()
        with self.assertRaises(core.BudgetError):client._pause(60)
        self.assertLess(time.monotonic()-start,.2)

    def test_api_scope_rejects_private_or_arbitrary_paths(self):
        for url in ('https://www.ozon.ru/my/orderlist','https://example.com/category/a/'):
            with self.assertRaises(core.ParseError):refresh.api_request(url)

    def test_incomplete_baseline_refused_before_any_browser_request(self):
        with patch.object(refresh.core,'load_zones',return_value=([ZONE],[])),\
             patch.object(refresh.core,'load_json',return_value=[dict(CAT,scope='food')]),\
             patch.object(refresh,'Baseline',side_effect=core.ParseError('incomplete baseline')),\
             patch('run_parser.preflight') as preflight,patch.object(refresh,'BrowserTransport') as transport:
            paths=[]
            for name in ('zones.csv','categories.json'):
                file=self.root/name;file.write_text('fixture');paths.append(str(file))
            result=refresh.main(['--baseline',str(self.root),'--zones',paths[0],'--categories',paths[1],'--plan'])
        self.assertEqual(result,2);preflight.assert_not_called();transport.assert_not_called()

    def manual_fixture(self,expired=False):
        results=self.root/'results';folder=results/'refresh_fixture';folder.mkdir(parents=True)
        core.write_json(results/'active_refresh_run.json',{'out_dir':str(folder),
            'started_unix':time.time()-(7300 if expired else 1),'seconds':7200})
        core.write_json(folder/'run_status.json',{'requires_manual_access_check':True,
            'manual_required_since_unix':time.time()-60})
        return results,folder

    def test_manual06_signals_existing_refresh_without_starting_another_process(self):
        _,folder=self.manual_fixture()
        with patch('run_parser.preflight') as preflight,patch.object(refresh,'run_refresh') as run:
            self.assertTrue(refresh.signal_refresh_manual(self.root,self.args.cdp))
        preflight.assert_called_once();run.assert_not_called()
        self.assertTrue((folder/'manual_access_restored.json').is_file())

    def test_expired_refresh_manual06_cannot_extend_or_request_access(self):
        _,folder=self.manual_fixture(expired=True)
        with patch('run_parser.preflight') as preflight:
            self.assertTrue(refresh.signal_refresh_manual(self.root,self.args.cdp))
        preflight.assert_not_called();self.assertFalse((folder/'manual_access_restored.json').exists())

    def test_newer_full_manual_episode_takes_priority_over_stale_refresh(self):
        results,_=self.manual_fixture()
        folder=results/'full';folder.mkdir()
        core.write_json(results/'active_shared_run.json',{'out_dir':str(folder)})
        core.write_json(folder/'backoff_status.json',{'manual_required_since_unix':time.time()})
        with patch('run_parser.preflight') as preflight:
            self.assertFalse(refresh.signal_refresh_manual(self.root,self.args.cdp))
        preflight.assert_not_called()

    def test_new_pass_inherits_cooldown_and_manual_requirement(self):
        results=self.root/'results';source=results/'full';source.mkdir(parents=True)
        output=results/'refresh';output.mkdir()
        state={'retry_at_unix':time.time()+100,'manual_required_since_unix':time.time()-5,
            'request_interval_seconds':60,'consecutive_failures':4}
        core.write_json(source/'backoff_status.json',state)
        refresh.inherit_access_state(output,source,results)
        self.assertEqual(core.load_json(output/'backoff_status.json'),state)


if __name__=='__main__':unittest.main()
