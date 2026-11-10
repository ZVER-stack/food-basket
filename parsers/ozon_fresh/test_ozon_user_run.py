import copy
import unittest
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock,patch
import ozon_fresh_fast as core
import run_parser
from ozon_run_lock import profile_lock

FIXTURE=core.load_json(Path(__file__).parent/'testdata/current_product_price.json')['data']

class UserLaunchChecks(unittest.TestCase):
    def test_manual_resume_uses_current_snapshot_and_expired_run_cannot_be_reset(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root=Path(directory);folder=root/'full_shared_today'
            core.write_json(root/'active_shared_run.json',{'out_dir':str(folder.resolve()),'started_unix':time.time(),'complete':False})
            self.assertEqual(run_parser.shared_output(root,True),folder.resolve())
            core.write_json(root/'active_shared_run.json',{'out_dir':str(folder.resolve()),'started_unix':time.time()-7300,'complete':False})
            with self.assertRaises(core.BudgetError):run_parser.shared_output(root,True)
            self.assertNotEqual(run_parser.shared_output(root),folder.resolve())

    def setUp(self):
        self.data=copy.deepcopy(FIXTURE)
        self.data['widgetStates']['profileMenuAnonymous-fixture']={}
        self.page=Mock()
        self.page.url='https://www.ozon.ru/product/moloko-parmalat-2952307571/'
        self.page.title.return_value='Молоко Parmalat'
        self.browser=Mock()
        self.browser.contexts=[Mock(pages=[self.page])]
        self.runtime=Mock()
        self.runtime.__enter__=Mock(return_value=self.runtime)
        self.runtime.__exit__=Mock(return_value=False)
        self.runtime.chromium.connect_over_cdp.return_value=self.browser

    def check(self,**options):
        with patch.object(run_parser,'sync_playwright',return_value=self.runtime),patch.object(run_parser.tiles,'document_states',return_value=self.data):
            return run_parser.preflight('http://127.0.0.1:9222',**options)

    def test_reads_existing_anonymous_product_without_site_requests(self):
        report=self.check()
        self.assertEqual(report['prices'],{'price':'199','price_regular':'215','price_card':'195'})
        self.assertEqual(report['network_requests'],0)
        self.page.goto.assert_not_called()
        self.page.evaluate.assert_not_called()

    def test_logged_in_profile_is_not_used(self):
        self.data['widgetStates']['profileMenu-fixture']={}
        with self.assertRaises(core.ParseError):self.check()

    def test_challenge_is_not_treated_as_ready(self):
        self.page.title.return_value='Antibot Captcha'
        with self.assertRaises(core.ProtectionError):self.check()
    def test_observed_no_connection_screen_is_not_treated_as_product_or_captcha(self):
        self.page.title.return_value='Похоже, нет соединения'
        with self.assertRaisesRegex(core.ProtectionError,'Нужна ручная проверка доступа'):
            self.check()
        self.page.content.assert_not_called()
        self.page.goto.assert_not_called();self.page.evaluate.assert_not_called()

    def test_multiple_ozon_tabs_require_explicit_choice(self):
        self.browser.contexts[0].pages.append(self.page)
        with self.assertRaises(core.ParseError):self.check()

    def test_known_test_product_is_selected_beside_fresh_home(self):
        home=Mock(url='https://www.ozon.ru/category/supermarket-25000/')
        self.browser.contexts[0].pages.insert(0,home)
        self.assertEqual(self.check()['product_id'],run_parser.TEST_SKU)
        home.content.assert_not_called()

    def test_manual_confirmation_accepts_equal_duplicate_test_tabs_in_same_session(self):
        duplicate=Mock(url=self.page.url);duplicate.title.return_value='Молоко Parmalat'
        self.browser.contexts[0].pages.append(duplicate)
        report=self.check(allow_duplicate_test_tabs=True)
        self.assertEqual(report['older_same_session_test_tabs_ignored'],1)
        self.assertEqual(report['network_requests'],0)
        duplicate.goto.assert_not_called();duplicate.content.assert_called_once()
        self.page.content.assert_not_called()

    def test_manual_confirmation_rejects_same_product_in_different_sessions(self):
        duplicate=Mock(url=self.page.url)
        self.browser.contexts.append(Mock(pages=[duplicate]))
        with self.assertRaises(core.ParseError):self.check(allow_duplicate_test_tabs=True)

    def test_manual_confirmation_reads_newest_test_tab_and_ignores_stale_old_html(self):
        duplicate=Mock(url=self.page.url);duplicate.title.return_value='Молоко'
        self.browser.contexts[0].pages.append(duplicate)
        self.page.title.return_value='Old stale document'
        report=self.check(allow_duplicate_test_tabs=True)
        self.assertEqual(report['product_id'],run_parser.TEST_SKU)
        self.page.content.assert_not_called();duplicate.content.assert_called_once()

    def test_second_collector_cannot_change_the_same_profile_address(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path=Path(directory)/'browser.lock'
            with profile_lock(path):
                with self.assertRaises(core.ParseError):
                    with profile_lock(path):pass
            with profile_lock(path):pass

    def test_failed_run_releases_the_profile_lock(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path=Path(directory)/'browser.lock'
            with self.assertRaises(RuntimeError):
                with profile_lock(path):raise RuntimeError('stopped')
            with profile_lock(path):pass

if __name__=='__main__':unittest.main()
