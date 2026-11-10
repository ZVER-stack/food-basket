import tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import ozon_fresh_fast as core
import run_parser,sys,time
from ozon_live_parser import start_full_deadline_timer


class FullDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.event=threading.Event()
        self.args=SimpleNamespace(mode='full',seconds=7200,continue_until_finished=False)

    def test_new_run_reserves_export_time_and_does_not_wait_beyond_cooldown_deadline(self):
        timer=Mock()
        with patch('ozon_live_parser.time.time',return_value=1000),patch('ozon_live_parser.threading.Timer',return_value=timer) as factory:
            result=start_full_deadline_timer(self.args,self.root,self.event)
        self.assertIs(result,timer);factory.assert_called_once_with(7140,self.event.set)
        self.assertEqual(timer.full_deadline_unix,8200);timer.start.assert_called_once()

    def test_resume_uses_original_snapshot_start_including_all_pauses(self):
        state=core.State(self.root/'snapshot.sqlite','fp')
        stamp='2026-10-18 12:00:00'
        state.db.execute("UPDATE meta SET value=? WHERE key='started_at'",(stamp,));state.db.commit();state.db.close()
        epoch=core.datetime.strptime(stamp,'%Y-%m-%d %H:%M:%S').replace(tzinfo=core.MSK).timestamp()
        with patch('ozon_live_parser.time.time',return_value=epoch+3600),patch('ozon_live_parser.threading.Timer') as factory:
            start_full_deadline_timer(self.args,self.root,self.event)
        factory.assert_called_once_with(3540,self.event.set)

    def test_expired_absolute_deadline_fires_immediately_without_extension(self):
        self.args.absolute_deadline=900
        with patch('ozon_live_parser.time.time',return_value=1000),patch('ozon_live_parser.threading.Timer') as factory:
            start_full_deadline_timer(self.args,self.root,self.event)
        factory.assert_called_once_with(0,self.event.set)

    def test_explicit_unlimited_continuation_does_not_get_a_hidden_two_hour_timer(self):
        self.args.continue_until_finished=True
        with patch('ozon_live_parser.threading.Timer') as factory:
            self.assertIsNone(start_full_deadline_timer(self.args,self.root,self.event))
        factory.assert_not_called()

    def old_run_pointer(self):
        results=self.root/'results';results.mkdir()
        old=results/'old'
        core.write_json(results/'active_shared_run.json',{'out_dir':str(old),'started_unix':time.time()-8000,
            'seconds':7200,'complete':False})
        return old

    def test_wrapper_returns_at_deadline_instead_of_waiting_for_manual_access_again(self):
        old=self.old_run_pointer();seen=[]
        ready={'product_url':'https://www.ozon.ru/product/milk-2952307571/','prices':{
            'price':'199','price_regular':'215','price_card':'195'},'address':'House'}
        def run(args):
            seen.append(args)
            if len(seen)>1:raise AssertionError('Bounded pass restarted after deadline')
            core.write_json(Path(args.out_dir)/'run_status.json',{'status':'deadline_finished',
                'collection_budget_exhausted':True,'requires_manual_access_check':True})
            return 2
        argv=['run_parser.py','--action','full','--price-workers','4','--catalog-workers','4','--wait-on-403']
        with patch.object(run_parser,'ROOT',self.root),patch.object(sys,'argv',argv),\
             patch.object(run_parser,'preflight',return_value=ready),patch.object(run_parser.live,'run',side_effect=run),\
             patch('ozon_backoff.BackoffGate') as gate,patch('ozon_status.show_status'):
            self.assertEqual(run_parser.main(),2)
        self.assertEqual(len(seen),1);self.assertEqual(seen[0].reference_snapshot,str(old))
        gate.assert_not_called()

    def test_bounded_preflight_challenge_aborts_without_an_unbounded_wait(self):
        self.old_run_pointer()
        argv=['run_parser.py','--action','full','--price-workers','4','--wait-on-403']
        with patch.object(run_parser,'ROOT',self.root),patch.object(sys,'argv',argv),\
             patch.object(run_parser,'preflight',side_effect=core.ProtectionError('CAPTCHA')),\
             patch.object(run_parser.live,'run') as run,patch('ozon_backoff.BackoffGate') as gate:
            self.assertEqual(run_parser.main(),2)
        gate.assert_not_called();run.assert_not_called()

    def test_manual06_cannot_turn_an_expired_bounded_run_into_unlimited_collection(self):
        old=self.old_run_pointer()
        core.write_json(old/'run_status.json',{'status':'deadline_finished','collection_budget_exhausted':True})
        argv=['run_parser.py','--action','full','--price-workers','4','--resume-after-manual-check',
            '--continue-until-finished','--wait-on-403']
        with patch.object(run_parser,'ROOT',self.root),patch.object(sys,'argv',argv),\
             patch.object(run_parser,'preflight') as preflight,patch.object(run_parser.live,'run') as run:
            self.assertEqual(run_parser.main(),2)
        preflight.assert_not_called();run.assert_not_called()


if __name__=='__main__':unittest.main()
