import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import ozon_fresh_fast as core
from ozon_metadata_backfill import run_with_network_recovery
from playwright.sync_api import Error as PlaywrightError

class MetadataRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.snapshot=self.root/'snapshot';self.snapshot.mkdir()
        self.auth=self.root/'authorization.json';self.write_auth(True)
        self.args=SimpleNamespace(snapshot=str(self.snapshot),job_dir=str(self.root/'job'),authorization=str(self.auth))
        self.now=1000.0
    def write_auth(self,active):
        self.auth.write_text(json.dumps({'active':active,'task_scope':'metadata_backfill_only'}))
    def sleep(self,seconds):self.now+=seconds
    def run_retry(self,execute):
        return run_with_network_recovery(self.args,execute,clock=lambda:self.now,sleep=self.sleep)
    def timeout(self):return core.ParseError('Browser request failed or timed out; no automatic retry')
    def test_only_timeout_resumes_after_full_drain_and_keeps_checkpoint(self):
        saved=self.root/'saved.json';saved.write_text('{"sku":"123","price":"100"}')
        execute=Mock(side_effect=[self.timeout(),7])
        self.assertEqual(self.run_retry(execute),7);self.assertEqual(self.now,1035)
        self.assertEqual(execute.call_count,2);self.assertEqual(saved.read_text(),'{"sku":"123","price":"100"}')
    def test_closed_target_reconnects_only_through_the_normal_verified_run(self):
        execute=Mock(side_effect=[PlaywrightError('BrowserContext.new_cdp_session: Target page, context or browser has been closed'),9])
        self.assertEqual(self.run_retry(execute),9);self.assertEqual(execute.call_count,2)
        self.assertEqual(self.now,1035)
    def test_other_browser_errors_are_not_blindly_retried(self):
        execute=Mock(side_effect=PlaywrightError('Protocol error: unknown method'))
        with self.assertRaises(PlaywrightError):self.run_retry(execute)
        self.assertEqual(execute.call_count,1)
    def test_protection_or_other_parse_error_never_retries(self):
        for error in (core.ProtectionError('CAPTCHA'),core.ParseError('Wrong house')):
            execute=Mock(side_effect=error)
            with self.assertRaises(type(error)):self.run_retry(execute)
            self.assertEqual(execute.call_count,1)
    def test_three_attempt_budget_is_persisted_and_cannot_loop_forever(self):
        execute=Mock(side_effect=self.timeout())
        with self.assertRaises(core.ParseError):self.run_retry(execute)
        self.assertEqual(execute.call_count,4)
        execute.reset_mock()
        with self.assertRaises(core.ParseError):self.run_retry(execute)
        self.assertEqual(execute.call_count,1)
        record=core.load_json(self.root/'job/network_recovery.json')
        self.assertTrue(record['exhausted']);self.assertEqual(record['attempts'],3)
    def test_new_manual_guard_during_drain_stops_before_second_call(self):
        execute=Mock(side_effect=self.timeout())
        def sleep(seconds):
            self.sleep(seconds)
            core.write_json(self.snapshot/'backoff_status.json',{'manual_required_since_unix':1001,'retry_at_unix':9999})
        with self.assertRaises(core.ProtectionError):
            run_with_network_recovery(self.args,execute,clock=lambda:self.now,sleep=sleep)
        self.assertEqual(execute.call_count,1)
        self.assertEqual(core.load_json(self.snapshot/'backoff_status.json')['retry_at_unix'],9999)
    def test_user_stop_during_drain_stops_before_second_call(self):
        execute=Mock(side_effect=self.timeout())
        def sleep(seconds):self.sleep(seconds);self.write_auth(False)
        with self.assertRaises(core.BudgetError):
            run_with_network_recovery(self.args,execute,clock=lambda:self.now,sleep=sleep)
        self.assertEqual(execute.call_count,1)

if __name__=='__main__':unittest.main()
