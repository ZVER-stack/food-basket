import copy
import unittest
from unittest.mock import Mock, patch
import ozon_fresh_fast as core
import ozon_har_catalog as tiles
from ozon_product_prices import enrich_prices, _enrich_prices_data, MissingAmountError
from test_ozon_product_prices import DATA


class AmountContinuationTests(unittest.TestCase):
    def setup_case(self):
        data=copy.deepcopy(DATA)
        for key in list(data['widgetStates']):
            if key.startswith(('webCharacteristics-','webShortCharacteristics-')):
                del data['widgetStates'][key]
        name='Универсальное средство Доместос спрей'
        tiles.widget(data,'webProductHeading-')['title']=name
        data['widgetStates']['addressBookBarWeb-test']={'customCell':{'actionMode':'common','cells':[{'text':{'text':'Бакунинская ул., 60/2'}}]}}
        data['widgetStates']['webAddToCart-test']={'firstButton':{'sku':'123','toCart':{'testInfo':{'automatizationId':'saleblock-add-to-cart-button-express'},'action':{'id':'addToCart','params':{'sku':'123'}}}}}
        data['widgetStates']['paginator-test']={'nextPage':'/product/test-123/?layout_container=pdpPage2&layout_page_index=2'}
        item={'product_id':'123','name':name,'url':'https://www.ozon.ru/product/test-123/','amount':None,'price':None,'extra':{'product_details_required':True}}
        detail={'widgetStates':{'webCharacteristics-test':{'characteristics':[{'title':'Объем, мл','values':[{'text':'500'}]}]}}}
        return data,detail,item,{'address':'Москва, Бакунинская улица, 60/2'}

    def test_amount_on_full_characteristics_is_read_before_failure(self):
        data,detail,item,zone=self.setup_case()
        with self.assertRaises(MissingAmountError):
            _enrich_prices_data(data,zone,item)
        with patch('ozon_product_prices.product_request',side_effect=[data,detail]) as request:
            result=enrich_prices(Mock(),zone,item)
        self.assertEqual(result['amount'],'500 мл')
        self.assertEqual(request.call_count,2)
        self.assertTrue(result['extra']['ordinary_price_verified_for_address'])
        self.assertEqual(result['extra']['product_metadata_schema'],2)

    def test_missing_full_amount_is_never_guessed(self):
        data,detail,item,zone=self.setup_case()
        with patch('ozon_product_prices.product_request',side_effect=[data,{'widgetStates':{}}]):
            with self.assertRaises(MissingAmountError):enrich_prices(Mock(),zone,item)

    def test_other_sku_continuation_is_rejected(self):
        data,detail,item,zone=self.setup_case()
        data['widgetStates']['paginator-test']['nextPage']='/product/other-999/?layout_container=pdpPage2&layout_page_index=2'
        with patch('ozon_product_prices.product_request',return_value=data) as request:
            with self.assertRaises(core.ParseError):enrich_prices(Mock(),zone,item)
        self.assertEqual(request.call_count,1)

    def test_full_characteristics_name_schema_reads_amount_and_brand(self):
        data,detail,item,zone=self.setup_case()
        detail={'widgetStates':{'webCharacteristics-test':{'characteristics':[{'short':[
            {'name':'Объем, мл','values':[{'text':'500'}]},
            {'name':'Бренд','values':[{'text':'Доместос'}]}]}]}}}
        with patch('ozon_product_prices.product_request',side_effect=[data,detail]):
            result=enrich_prices(Mock(),zone,item)
        self.assertEqual(result['amount'],'500 мл')
        self.assertEqual(result['brand'],'Доместос')
