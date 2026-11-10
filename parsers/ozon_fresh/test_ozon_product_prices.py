import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
import ozon_fresh_fast as core
import ozon_har_catalog as tiles
from ozon_browser_transport import BrowserTransport
from ozon_product_prices import product_prices, enrich_prices, fresh_product_offer
from ozon_live_parser import full_config

FIXTURE = core.load_json(Path(__file__).parent/'testdata/legacy_product_price.json')
DATA = FIXTURE['data']
NAME = tiles.widget(DATA, 'webProductHeading-')['title']

class LegacyPriceTests(unittest.TestCase):
    def test_actual_saved_response_matches_original_prices(self):
        self.assertFalse(FIXTURE['synthetic'])
        self.assertEqual(product_prices(DATA, NAME), {'price':'179', 'price_regular':'251', 'price_card':None})

    def test_explicit_card_price_keeps_all_three_roles_separate(self):
        data = copy.deepcopy(DATA)
        tiles.widget(data, 'webPrice-')['cardPrice'] = '150 ₽'
        self.assertEqual(product_prices(data, NAME), {'price':'179', 'price_regular':'251', 'price_card':'150'})

    def test_recommendation_price_cannot_fill_missing_product_price(self):
        data = copy.deepcopy(DATA)
        data['widgetStates'].pop('webPrice-fixture')
        data['widgetStates']['skuScroll-fixture'] = {'cardPrice':99, 'price':105}
        with self.assertRaises(core.ParseError):
            product_prices(data, NAME)

    def test_conflicting_primary_prices_stop_instead_of_selecting_one(self):
        data = copy.deepcopy(DATA)
        data['widgetStates']['webPrice-other'] = {'price':'180 ₽', 'originalPrice':'251 ₽'}
        with self.assertRaises(core.ParseError):
            product_prices(data, NAME)

    def test_other_product_heading_is_rejected(self):
        with self.assertRaises(core.ParseError):
            product_prices(DATA, 'Другой товар')

    def test_missing_original_price_uses_ordinary_current_price(self):
        data = copy.deepcopy(DATA)
        del tiles.widget(data, 'webPrice-')['originalPrice']
        self.assertEqual(product_prices(data, NAME)['price_regular'],'179')

    def test_enrichment_keeps_catalog_card_quote_in_provenance_when_product_has_none(self):
        data = copy.deepcopy(DATA)
        data['widgetStates']['addressBookBarWeb-fixture'] = {'customCell':{'actionMode':'common','cells':[{'text':{'text':'Бакунинская ул., 60/2'}}]}}
        client = Mock(); client.product_page.return_value = data
        data['widgetStates']['webAddToCart-fixture'] = {'firstButton':{'sku':'123','toCart':{'testInfo':{'automatizationId':'saleblock-add-to-cart-button-express'},'action':{'id':'addToCart','params':{'sku':'123'}}}}}
        row = {'product_id':'123','name':NAME,'url':'https://www.ozon.ru/product/test-123/','price':None,'price_card':150,'extra':{}}
        result = enrich_prices(client, {'address':'Москва, Бакунинская улица, 60/2'}, row)
        self.assertEqual((result['price'],result['price_regular'],result['price_card']),('179','251',None))
        self.assertEqual(result['extra']['catalog_prices']['price_card'],150)
        self.assertIsNone(row['price'])

    def test_enrichment_requires_house_confirmation(self):
        client = Mock(); client.product_page.return_value = DATA
        with self.assertRaises(core.ParseError):
            enrich_prices(client, {'address':'Москва, Бакунинская улица, 60/2'}, {'name':NAME,'url':'https://www.ozon.ru/product/test-123/','extra':{}})

    def test_product_navigation_403_stops_before_api_and_preserves_root_tab(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            client = BrowserTransport.__new__(BrowserTransport)
            client.state_path, client.state = Path(directory)/'state.json', {}
            client.deadline,client.interval,client.count,client.max_requests = time.monotonic()+60,3,0,10
            root = client.page = Mock()
            tab = root
            tab.goto.return_value.status = 403
            client.entrypoint = Mock()
            with self.assertRaises(core.ProtectionError):
                client.product_page('https://www.ozon.ru/product/test-123/')
            self.assertTrue(client.state['blocked'])
            self.assertIs(client.page,root)
            client.entrypoint.assert_not_called()
            tab.close.assert_not_called()
            root.context.new_page.assert_not_called()

    def test_enrichment_protection_error_is_not_swallowed_as_bad_card(self):
        fixture = core.load_json(Path(__file__).parent/'testdata/public_tiles.json')
        tile = fixture['samples']['CARD_PRICE']
        client = Mock(); client.request.return_value = {'widgetStates':{'tileGridDesktop-fixture':{'items':[tile]},'infiniteVirtualPaginator-fixture':{'nextPage':None}}}
        categories = [{'category_id':'123','name':'Тест','url':'https://www.ozon.ru/category/test-123/?miniapp=supermarket'}]
        enrich = Mock(side_effect=core.ProtectionError('HTTP 403'))
        with self.assertRaises(core.ProtectionError):
            client.request.return_value['layout']=[{'component':'tileGridDesktop','stateId':'tileGridDesktop-fixture','widgetTrackingInfo':{'name':'catalog.searchResultsV2'}}]
            core.collect_catalog(client,full_config(categories),{},enrich_product=enrich)
        self.assertEqual(client.request.call_count,1)

    def test_repeated_same_house_offer_does_not_request_product_twice(self):
        fixture = core.load_json(Path(__file__).parent/'testdata/public_tiles.json')
        tile = fixture['samples']['CARD_PRICE']
        client = Mock(); client.request.return_value = {'widgetStates':{'tileGridDesktop-fixture':{'items':[tile]},'infiniteVirtualPaginator-fixture':{'nextPage':None}}}
        categories = [{'category_id':str(cid),'name':'Тест','url':f'https://www.ozon.ru/category/test-{cid}/?miniapp=supermarket'} for cid in (123,124)]
        enrich = Mock(side_effect=lambda item: dict(item,price='200',price_regular=item['price_regular'] or '250'))
        client.request.return_value['layout']=[{'component':'tileGridDesktop','stateId':'tileGridDesktop-fixture','widgetTrackingInfo':{'name':'catalog.searchResultsV2'}}]
        _, products = core.collect_catalog(client,full_config(categories),{},enrich_product=enrich)
        self.assertEqual(len(products),1)
        self.assertEqual(enrich.call_count,1)

    def test_catalog_page_cannot_use_another_house_header(self):
        fixture=core.load_json(Path(__file__).parent/'testdata/public_tiles.json')
        client=Mock();client.request.return_value={'widgetStates':{
            'tileGridDesktop-fixture':{'items':[fixture['samples']['ACTUAL_PRICE']]},
            'infiniteVirtualPaginator-fixture':{'nextPage':None},
            'addressBookBarWeb-fixture':{'customCell':{'actionMode':'common','cells':[{'text':{'text':'Бакунинская ул., 60/2'}}]}}}}
        cats=[{'category_id':'123','name':'Тест','url':'https://www.ozon.ru/category/test-123/?miniapp=supermarket'}]
        with self.assertRaises(core.ParseError):
            core.collect_catalog(client,full_config(cats),{'verified_address':'Новощукинская ул., 2'})

    def test_repeated_weighted_offer_does_not_compare_pack_price_to_kg_price(self):
        tile=copy.deepcopy(core.load_json(Path(__file__).parent/'testdata/unit_price_tile.json')['card'])
        next(atom['priceV2'] for atom in tile['mainState'] if atom['type']=='priceV2')['priceStyle']['styleType']='SALE_PRICE'
        client=Mock();client.request.return_value={'widgetStates':{'tileGridDesktop-fixture':{'items':[tile]},'infiniteVirtualPaginator-fixture':{'nextPage':None}}}
        cats=[{'category_id':str(cid),'name':'Тест','url':f'https://www.ozon.ru/category/test-{cid}/?miniapp=supermarket'} for cid in (123,124)]
        enrich=Mock(side_effect=lambda item:dict(item,price='236',price_regular='269',price_card=None))
        client.request.return_value['layout']=[{'component':'tileGridDesktop','stateId':'tileGridDesktop-fixture','widgetTrackingInfo':{'name':'catalog.searchResultsV2'}}]
        _,products=core.collect_catalog(client,full_config(cats),{},enrich_product=enrich)
        self.assertEqual(len(products),1)
        self.assertEqual(products[0]['price'],'236')


class CurrentPublicProductTests(unittest.TestCase):
    def setUp(self):
        self.fixture = core.load_json(Path(__file__).parent/'testdata/current_product_price.json')
        self.data = copy.deepcopy(self.fixture['data'])
        self.name = tiles.widget(self.data,'webProductHeading-')['title']

    def test_actual_current_capture_has_three_separate_prices(self):
        self.assertFalse(self.fixture['synthetic'])
        self.assertEqual(product_prices(self.data,self.name), {'price':'199','price_regular':'215','price_card':'195'})

    def test_current_capture_proves_fresh_offer(self):
        self.assertTrue(fresh_product_offer(self.data,'2952307571'))

    def test_another_sku_cannot_use_the_captured_product_offer(self):
        with self.assertRaises(core.ParseError):
            fresh_product_offer(self.data,'999999')

    def test_current_capture_enriches_only_the_confirmed_house(self):
        client = Mock(); client.product_page.return_value = self.data
        row = {'product_id':'2952307571','name':self.name,'url':'https://www.ozon.ru/product/test-2952307571/','price_card':'195','extra':{}}
        result = enrich_prices(client,{'address':'Москва, Новощукинская улица, 2'},row)
        self.assertEqual((result['price'],result['price_regular'],result['price_card']),('199','215','195'))
        with self.assertRaises(core.ParseError):
            enrich_prices(client,{'address':'Москва, Бакунинская улица, 60/2'},row)


class WeightedProductQuoteTests(unittest.TestCase):
    def setUp(self):
        self.data=copy.deepcopy(core.load_json(Path(__file__).parent/'testdata/weighted_product_price.json')['data'])
        self.item=tiles.tile_product(core.load_json(Path(__file__).parent/'testdata/unit_price_tile.json')['card'])
        self.client=Mock();self.client.product_page.return_value=self.data
        self.zone={'address':'Москва, Бакунинская улица, 60/2'}

    def test_reads_actual_primary_prices_without_mass_calculation(self):
        row=enrich_prices(self.client,self.zone,self.item)
        self.assertEqual((row['price'],row['price_regular'],row['price_card']),('236','269','224'))
        self.assertEqual(row['amount'],'1-1.50 кг')
        self.assertEqual((row['unit_price'],row['unit_price_basis']),('150','1 кг'))
        self.assertEqual(row['extra']['catalog_unit_price'],'149')

    def test_missing_unit_basis_does_not_allow_guessing_a_conversion(self):
        del tiles.widget(self.data,'webPrice-')['measurePerUnit']
        with self.assertRaises(core.ParseError):enrich_prices(self.client,self.zone,self.item)

    def test_different_unit_basis_is_rejected(self):
        tiles.widget(self.data,'webPrice-')['measurePerUnit']='за 1 л'
        with self.assertRaises(core.ParseError):enrich_prices(self.client,self.zone,self.item)

    def test_same_sku_house_price_difference_uses_one_product_quote_and_preserves_catalog(self):
        self.item.pop('unit_price_basis')
        row=enrich_prices(self.client,self.zone,self.item)
        self.assertEqual((row['price'],row['price_regular'],row['price_card']),('236','269','224'))
        self.assertEqual(row['extra']['catalog_prices']['price_card'],'149')
        self.assertTrue(row['extra']['catalog_product_prices_differ'])

if __name__ == '__main__':
    unittest.main()
