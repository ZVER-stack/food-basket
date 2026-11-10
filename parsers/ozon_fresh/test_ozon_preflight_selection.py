import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,MagicMock,patch
import ozon_fresh_fast as core
import run_parser

class PreflightSelectionTests(unittest.TestCase):
    def setUp(self):
        self.pasta=Mock();self.pasta.url='https://www.ozon.ru/product/pasta-4320037222/?sh=published'
        self.pasta.title.return_value='Pasta';self.pasta.content.return_value='published HTML'
        self.milk=Mock();self.milk.url='https://www.ozon.ru/product/milk-2952307185/'
        self.context=SimpleNamespace(pages=[self.milk,self.pasta])
        self.browser=SimpleNamespace(contexts=[self.context])
        self.pw=Mock();self.pw.chromium.connect_over_cdp.return_value=self.browser
        self.manager=MagicMock();self.manager.__enter__.return_value=self.pw
        self.data={'widgetStates':{
            'profileMenuAnonymous-test':{},'webProductHeading-test':{'title':'Pasta'},
            'webPrice-test':{'price':'79','originalPrice':'105','cardPrice':'77'},
            'webAddToCart-test':{'firstButton':{'sku':'4320037222','toCart':{
                'testInfo':{'automatizationId':'saleblock-add-to-cart-button-express'},
                'action':{'id':'addToCart','params':{'sku':'4320037222'}}}}}}}
    def check(self,sku='4320037222'):
        with patch.object(run_parser,'sync_playwright',return_value=self.manager),patch.object(run_parser.tiles,'document_states',return_value=self.data):
            return run_parser.preflight('http://127.0.0.1:9222',True,preferred_sku=sku)
    def test_confirmed_product_selected_without_closing_other_task_tabs_or_requests(self):
        report=self.check();self.assertEqual(report['product_id'],'4320037222')
        self.assertEqual(report['network_requests'],0);self.assertEqual(report['prices']['price_card'],'77')
        for page in (self.pasta,self.milk):page.goto.assert_not_called();page.close.assert_not_called();page.evaluate.assert_not_called()
        self.milk.content.assert_not_called()
    def test_missing_preferred_sku_does_not_silently_select_another_tab(self):
        with self.assertRaises(core.ParseError):self.check('999')
    def test_multiple_browser_contexts_are_rejected(self):
        self.browser.contexts=[SimpleNamespace(pages=[self.milk]),SimpleNamespace(pages=[self.pasta])]
        with self.assertRaises(core.ParseError):self.check()
    def test_current_challenge_on_selected_document_is_not_acknowledged(self):
        self.pasta.title.return_value='CAPTCHA'
        with self.assertRaises(core.ProtectionError):self.check()
    def test_invalid_preferred_identifier_is_rejected(self):
        with self.assertRaises(core.ParseError):self.check('123/other')

    def test_check_launcher_uses_requested_open_sku_without_starting_a_collection(self):
        report={'prices':{'price':'79','price_regular':'105','price_card':'77'},'address':'House1'}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            with patch.object(run_parser,'ROOT',Path(folder)),patch('sys.argv',['run_parser.py','--action','check','--preferred-sku','4320037222']),patch.object(run_parser,'preflight',return_value=report) as check,patch.object(run_parser.live,'run') as collect:
                self.assertEqual(run_parser.main(),0)
            check.assert_called_once_with('http://127.0.0.1:9222',allow_duplicate_test_tabs=True,preferred_sku='4320037222')
            collect.assert_not_called()

if __name__=='__main__':unittest.main()
