import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
import ozon_fresh_fast as core
import ozon_har_catalog as tiles
from ozon_catalog_index import CatalogIndex, needs_product, extra_of
from ozon_product_prices import product_metadata, _enrich_prices_data
from ozon_live_parser import full_config
from test_ozon_catalog_index import saved, card, CAT

class MetadataStrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.state = core.State(Path(self.tmp.name)/'snapshot.sqlite','fixture')
        self.addCleanup(self.state.db.close)
        self.index = CatalogIndex(self.state,allow_price_reuse=True,metadata_strategy=True)
        self.old = saved()
        self.old.update(description='Saved description',protein='8',composition='Milk')
        extra = extra_of(self.old); extra['product_metadata_observed_at']='2026-10-18 12:00:00'; extra['product_metadata_schema']=2
        self.old['extra']=extra
        self.index.publish('A',[self.old])

    def test_same_prices_reuse_static_metadata_and_current_destination_offer(self):
        current=tiles.tile_product(card('1')); current['extra']['delivery_address']='House B'
        result=self.index.resolve('B',current,{})
        self.assertFalse(needs_product(result))
        self.assertEqual(result['description'],'Saved description')
        self.assertEqual(result['protein'],'8')
        self.assertEqual(extra_of(result)['delivery_address'],'House B')
        self.assertNotIn('h3_index',result)
        self.assertTrue(extra_of(result)['ordinary_price_inferred'])

    def test_changed_price_requests_card_even_with_explicit_catalog_ordinary_price(self):
        current=tiles.tile_product(card('1',bank='500'))
        current['price']='520'
        result=self.index.resolve('B',current,{})
        self.assertTrue(needs_product(result))
        self.assertEqual(result['price_card'],'500')
        self.assertNotEqual(result['price'],self.old['price'])

    def test_new_sku_requests_card_even_with_complete_catalog_prices(self):
        current=tiles.tile_product(card('2')); current['price']='419'
        self.assertTrue(needs_product(self.index.resolve('B',current,{})))

    def test_legacy_without_observed_characteristics_is_read_once(self):
        self.index.metadata_reference.clear()
        result=self.index.resolve('B',tiles.tile_product(card('1')),{})
        self.assertTrue(needs_product(result))
        self.index.remember_product('B',self.old)
        self.assertFalse(needs_product(self.index.resolve('B',tiles.tile_product(card('1')),{})))

    def test_name_or_amount_conflict_requires_product(self):
        for field,value in [('name','Different product'),('amount','900 г')]:
            current=tiles.tile_product(card('1')); current[field]=value
            with self.subTest(field=field):
                self.assertTrue(needs_product(self.index.resolve('B',current,{})))

    def test_primary_characteristics_are_exported_without_recommendation_data(self):
        data={'widgetStates':{
            'webCharacteristics-test':{'items':[
                {'title':'Белки, г','values':[{'text':'8,5'}]},
                {'title':'Состав','values':[{'text':'Milk'}]},
                {'title':'Энергетическая ценность, ккал','values':[{'text':'от 50 до 80'}]}]},
            'webDescription-test':{'html':'<p>Primary description</p>'},
            'recommendations-test':{'title':'Жиры, г','values':[{'text':'99'}]}}}
        result,characteristics=product_metadata(data)
        self.assertEqual(result['protein'],'8.5')
        self.assertEqual(result['description'],'Primary description')
        self.assertNotIn('fat',result); self.assertNotIn('kcal',result)
        item=tiles.tile_product(card('1')); item.update(result)
        row=core.card_row(item|{'price':'419'},full_config([CAT])['card_mapping'],CAT)
        self.assertEqual(row['description'],'Primary description')
        self.assertEqual(row['protein'],'8.5')

    def test_nutrition_block_and_published_product_description_continuation(self):
        from ozon_product_prices import enrich_prices
        fixture=core.load_json(Path(__file__).parent/'testdata/current_product_price.json')['data']
        fixture=copy.deepcopy(fixture)
        name=tiles.widget(fixture,'webProductHeading-')['title']
        sku='2952307571'
        fixture['widgetStates']['addressBookBarWeb-fixture']={'customCell':{'actionMode':'common','cells':[{'text':{'text':'House B'}}]}}
        fixture['widgetStates']['webAddToCart-fixture']={'firstButton':{'sku':sku,'toCart':{'testInfo':{'automatizationId':'saleblock-add-to-cart-button-express'},'action':{'id':'addToCart','params':{'sku':sku}}}}}
        fixture['widgetStates']['paginator-fixture']={'nextPage':f'/product/test-{sku}/?layout_container=pdpPage2column&layout_page_index=2'}
        details={'widgetStates':{'webNutritionInfo-fixture':{'values':[{'label':'белки','value':'5'},{'label':'ккал','value':'55'}]},'webDescription-fixture':{'html':'<p>Description from page two</p>'}}}
        client=Mock();client.product_page.return_value=fixture;client.entrypoint.return_value=details
        item={'product_id':sku,'name':name,'url':f'https://www.ozon.ru/product/test-{sku}/','amount':'1 л','extra':{'product_details_required':True}}
        result=enrich_prices(client,{'address':'House B'},item)
        self.assertEqual(result['protein'],'5');self.assertEqual(result['kcal'],'55')
        self.assertEqual(result['description'],'Description from page two')
        self.assertEqual(result['extra']['product_metadata_schema'],2)
        self.assertEqual(client.entrypoint.call_count,1)
        fixture['widgetStates']['paginator-fixture']['nextPage']='/product/other-123/?layout_container=pdpPage2column&layout_page_index=2'
        with self.assertRaises(core.ParseError): enrich_prices(client,{'address':'House B'},item)

if __name__=='__main__':unittest.main()
