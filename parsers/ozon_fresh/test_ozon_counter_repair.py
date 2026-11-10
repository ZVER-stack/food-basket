from pathlib import Path
import tempfile
import unittest
import ozon_fresh_fast as core
from ozon_repair_counter import correction,KNOWN_EPISODE


class CounterRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        state=core.State(self.folder/'snapshot.sqlite','fixture')
        for i in range(100):
            stamp=core.datetime.fromtimestamp(KNOWN_EPISODE-300+i*3,core.MSK).strftime('%Y-%m-%d %H:%M:%S')
            state.put_page('house','page'+str(i),stamp,[{'product_id':str(i)}],None)
        state.db.close()
        self.state=dict(last_failure_unix=KNOWN_EPISODE,consecutive_failures=7,
            retry_at_unix=KNOWN_EPISODE+1800,manual_required_since_unix=0,
            stop_reason='HTTP 403; increasing shared cooldown')
        self.worker=dict(blocked=True,last_status=403,blocked_at_unix=KNOWN_EPISODE,
            retry_after_seconds=0,reason='HTTP 403; increasing shared cooldown')
    def test_only_known_legacy_counter_is_repaired_and_audit_retains_original(self):
        plan=correction(self.folder,self.state,[self.worker])
        self.assertEqual(plan['after']['retry_at_unix'],KNOWN_EPISODE+120)
        self.assertEqual(plan['after']['consecutive_failures'],1)
        self.assertEqual(plan['after']['total_protection_failures'],7)
        self.assertEqual(plan['before'],self.state)
        self.assertTrue(self.worker['blocked'])
    def test_server_retry_after_is_not_shortened(self):
        plan=correction(self.folder,self.state,[dict(self.worker,retry_after_seconds=900)])
        self.assertEqual(plan['after']['retry_at_unix'],KNOWN_EPISODE+900)
        self.assertIsNone(correction(self.folder,self.state,[dict(self.worker,retry_after_seconds=2000)]))
    def test_manual_challenge_cannot_be_repaired(self):
        with self.assertRaises(core.ParseError):
            correction(self.folder,dict(self.state,manual_required_since_unix=KNOWN_EPISODE),[self.worker])
        with self.assertRaises(core.ParseError):
            correction(self.folder,self.state,[dict(self.worker,reason='CAPTCHA')])
    def test_unknown_episode_and_other_failure_are_not_repaired(self):
        self.assertIsNone(correction(self.folder,dict(self.state,last_failure_unix=KNOWN_EPISODE+1),[self.worker]))
        with self.assertRaises(core.ParseError):
            correction(self.folder,self.state,[dict(self.worker,last_status=429)])
    def test_absence_of_a_healthy_period_prevents_repair(self):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.folder/'snapshot.sqlite')) as db:
            db.execute('DELETE FROM pages');db.commit()
        self.assertIsNone(correction(self.folder,self.state,[self.worker]))


if __name__=='__main__':unittest.main()
