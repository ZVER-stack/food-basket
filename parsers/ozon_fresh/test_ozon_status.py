"""Offline acceptance checks for the independent launch and status workflow."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import ozon_fresh_fast as core
import ozon_status
import run_parser


class StandaloneWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.results=self.root/'results'
        self.folder=self.results/'full_shared_test';self.folder.mkdir(parents=True)
        core.write_json(self.results/'active_shared_run.json',dict(out_dir=str(self.folder.resolve()),
            started_unix=1000,seconds=7200,complete=False))
        state=core.State(self.folder/'snapshot.sqlite','fixture')
        state.mark('A','complete',[{'product_id':'1'}])
        state.mark('B','collected_with_gaps',[{'product_id':'2'},{'product_id':'3'}])
        state.mark('C','no_delivery',[]);state.db.close()
        self.zones=patch.object(core,'load_zones',return_value=([{}]*197,[{}]*4))
        self.zones.start();self.addCleanup(self.zones.stop)
    def test_local_status_keeps_complete_gapped_and_no_delivery_distinct(self):
        core.write_json(self.folder/'run_status.json',{'status':'finished_with_gaps'})
        data=ozon_status.read_status(self.root)
        self.assertEqual((data['requested_zones'],data['zones_processed']),(197,3))
        self.assertEqual((data['zones_complete'],data['zones_with_gaps'],data['zones_no_delivery']),(1,1,1))
        self.assertEqual(data['exported_zone_rows'],3)
    def test_dead_process_is_not_reported_as_running(self):
        core.write_json(self.folder/'run_status.json',{'status':'waiting_backoff','pid':123})
        with patch.object(ozon_status,'process_alive',return_value=False):
            self.assertEqual(ozon_status.read_status(self.root)['status'],'process_not_running')
    def test_stopped_status_distinguishes_old_rate_from_next_launch_policy(self):
        core.write_json(self.folder/'run_status.json', {'status':'stopped'})
        core.write_json(self.folder/'backoff_status.json', {'request_interval_seconds':5})
        core.write_json(self.root/'standalone_configuration.json', dict(
            interval_min_seconds=1.1,http_error_steps_seconds=[2,3,4]))
        with patch('builtins.print') as output:
            data=ozon_status.show_status(self.root)
        self.assertEqual(data['interval_seconds'],5)
        text='\n'.join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn('Интервал последнего запуска: 5',text)
        self.assertIn('1.1 → 2 → 3 → 4',text)
    def test_status_cannot_read_a_pointer_outside_results(self):
        core.write_json(self.results/'active_shared_run.json',{'out_dir':str(self.root.resolve())})
        with self.assertRaises(core.ParseError):ozon_status.read_status(self.root)
    def test_status_cli_never_connects_to_chrome_or_starts_collection(self):
        with patch.object(run_parser,'ROOT',self.root),patch('sys.argv',['run_parser','--action','status']),\
                patch.object(run_parser,'preflight') as preflight,patch.object(run_parser.live,'run') as run,\
                patch('builtins.print'):
            self.assertEqual(run_parser.main(),0)
        preflight.assert_not_called();run.assert_not_called()
    def test_full_cli_forwards_seven_readers_and_bounded_rate_and_honors_ctrl_c(self):
        def finish(args):
            core.write_json(self.folder/'run_status.json',dict(status='stopped',user_requested_stop=True,
                requires_manual_access_check=True))
            return 2
        ready=dict(prices=dict(price='10',price_regular='12',price_card='9'),address='A',
            product_url='https://www.ozon.ru/product/test-1/')
        launcher=(Path(__file__).parent/'08_continue_all.cmd').read_text(encoding='utf-8')
        command=next(line for line in launcher.splitlines() if line.startswith('"%FRESH_PY%"') and 'run_parser.py' in line)
        argv=['run_parser']+command.split()[2:]
        with patch.object(run_parser,'ROOT',self.root),patch('sys.argv',argv),\
                patch.object(run_parser,'preflight',return_value=ready),\
                patch.object(run_parser.live,'run',side_effect=finish) as run,\
                patch.object(ozon_status,'show_status'),patch('builtins.print'):
            self.assertEqual(run_parser.main(),2)
        run.assert_called_once()
        args=run.call_args.args[0]
        self.assertEqual((args.price_workers,args.catalog_workers,args.interval,args.max_interval),(7,7,5,10))
        self.assertEqual(args.price_policy,'catalog_metadata_cache')
        self.assertEqual(args.http_error_steps,(6,7,8,9,10))
        self.assertEqual(args.http_error_statuses,(403,429))
        self.assertIsNone(args.on_403_interval)
        self.assertIsNone(args.on_429_interval)
        self.assertEqual(args.limit,0);self.assertTrue(args.continue_until_finished)
        self.assertIsNone(args.absolute_deadline)
    def test_manual_confirmation_during_backoff_never_launches_a_duplicate(self):
        core.write_json(self.folder/'run_status.json',{'status':'waiting_backoff'})
        argv=['run_parser','--action','full','--resume-after-manual-check','--continue-until-finished']
        with patch.object(run_parser,'ROOT',self.root),patch('sys.argv',argv),\
                patch.object(run_parser,'preflight',return_value={}),\
                patch.object(run_parser.live,'run') as run,patch('builtins.print'):
            self.assertEqual(run_parser.main(),0)
        run.assert_not_called()
        self.assertTrue((self.folder/'manual_access_restored.json').exists())


if __name__=='__main__':unittest.main()
