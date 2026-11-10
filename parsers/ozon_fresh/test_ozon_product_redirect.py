import tempfile,unittest,json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote
import ozon_fresh_fast as core
from ozon_product_redirect import product_request,unavailable_redirect,UnavailableProduct

class ProductRedirectTests(unittest.TestCase):
    def location(self,sku='123',path='/search/'):
        return '/api/entrypoint-api.bx/page/json/v2?page_changed=true&url='+quote(path+'?deny_category_prediction=true&from_global=true&text=Food&product_id='+sku,safe='')
    def test_only_exact_published_same_sku_search302_is_unavailable(self):
        self.assertTrue(unavailable_redirect(302,self.location(),'123'))
        for status,location in [(403,self.location()),(0,self.location()),(302,self.location('456')),
            (302,self.location(path='/challenge.html')),(302,'https://other.example'+self.location()),
            (302,'/challenge.html'),(302,'/search/?product_id=123')]:
            self.assertFalse(unavailable_redirect(status,location,'123'))
    def client(self,root,location=None,manual=False):
        client=Mock();client.state={'last_status':0,'blocked':True,'reason':'HTTP 0; increasing shared cooldown','old_history':11}
        client.state_path=root/'transport.json';client.backoff_gate=SimpleNamespace(manual_since=123 if manual else 0)
        handlers={};session=Mock();session.on.side_effect=lambda key,handler:handlers.__setitem__(key,handler)
        client.page.context.new_cdp_session.return_value=session
        item={'product_id':'123','url':'https://www.ozon.ru/product/item-123/'}
        def call(url):
            handlers['Network.requestWillBeSent']({'requestId':'a','request':{'url':core.API+quote('/product/item-123/',safe=''),'method':'GET'}})
            if location:handlers['Network.responseReceivedExtraInfo']({'requestId':'a','statusCode':302,'headers':{'location':location,'set-cookie':'NEVER_SAVE'}})
            raise core.ProtectionError('HTTP 0; increasing shared cooldown')
        client.product_page.side_effect=call
        return client,item,session
    def test_observed_redirect_is_not_followed_and_history_is_preserved(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            client,item,session=self.client(Path(temp),self.location())
            with self.assertRaises(UnavailableProduct):product_request(client,item)
            self.assertFalse(client.state['blocked']);self.assertEqual(client.state['old_history'],11)
            client.product_page.assert_called_once();client.entrypoint.assert_not_called();session.detach.assert_called_once()
            self.assertNotIn('NEVER_SAVE',client.state_path.read_text())
    def test_unproven_http0_remains_blocked(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            client,item,_=self.client(Path(temp))
            with self.assertRaises(core.ProtectionError):product_request(client,item)
            self.assertTrue(client.state['blocked'])
    def test_another_readers_manual_guard_cannot_be_cleared(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            client,item,_=self.client(Path(temp),self.location(),manual=True)
            with self.assertRaises(core.ProtectionError):product_request(client,item)
            self.assertTrue(client.state['blocked']);self.assertEqual(client.backoff_gate.manual_since,123)
    def test_wrong_sku_url_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            client,item,_=self.client(Path(temp),self.location())
            with self.assertRaises(core.ParseError):product_request(client,item|{'product_id':'456'})
            client.product_page.assert_not_called()

    def test_main_price_enrichment_observes_same_sku_unavailable_redirect(self):
        from ozon_product_prices import enrich_prices
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            client,item,_=self.client(Path(temp),self.location())
            with self.assertRaises(UnavailableProduct):
                enrich_prices(client,{'address':'House1'},item)
            self.assertFalse(client.state['blocked'])
            client.product_page.assert_called_once()

    def test_main_price_enrichment_preserves_manual_guard(self):
        from ozon_product_prices import enrich_prices
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            client,item,_=self.client(Path(temp),self.location(),manual=True)
            with self.assertRaises(core.ProtectionError):
                enrich_prices(client,{'address':'House1'},item)
            self.assertTrue(client.state['blocked'])
            self.assertEqual(client.backoff_gate.manual_since,123)

if __name__=='__main__':unittest.main()
