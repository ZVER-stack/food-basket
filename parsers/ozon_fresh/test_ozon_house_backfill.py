import copy,csv,json,sqlite3,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import Mock
import ozon_fresh_fast as core
from ozon_house_export import MetadataStore,merge_row,identity,export,HEADERS,deduplicate
from ozon_metadata_backfill import read_metadata,apply_result,pick_sources,check_guards

class HouseExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.root=Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.snapshot=self.root/'snapshot';self.snapshot.mkdir()
        self.zones=self.root/'houses.csv'
        self.zones.write_text('h3_index,address\nA,House A\nB,House B\nC,House C\n',encoding='utf-8')
        self.row={'h3_index':'A','product_id':'1','name':'Milk','amount':'500 г','price':'100',
                  'price_regular':'120','price_card':'90','brand':'Brand','extra':{},'url':'https://www.ozon.ru/product/milk-1/'}
        self.db=sqlite3.connect(self.snapshot/'snapshot.sqlite')
        self.db.executescript('CREATE TABLE zones(h3,status,parsed_at,payload);CREATE TABLE pages(cache_key,url,captured_at,payload);')
        self.addCleanup(self.db.close)
    def zone(self,h3,row):
        self.db.execute('INSERT INTO zones VALUES (?,?,?,?)',(h3,'complete','2026-11-01',json.dumps([row])))
        self.db.commit()
    def test_export_preserves_two_houses_and_three_prices_as_cohort(self):
        first=copy.deepcopy(self.row);first.update(description='Site description',kcal='50',protein='0',fat='1',carbs='2')
        second=copy.deepcopy(self.row);second.update(h3_index='B',price='110',price_regular='150',price_card=None)
        self.zone('A',first);self.zone('B',second)
        store=MetadataStore(self.root/'meta.sqlite');store.seed(self.snapshot);store.close()
        before=(self.snapshot/'snapshot.sqlite').read_bytes()
        report=export(self.snapshot,self.zones,self.root/'meta.sqlite',self.root/'out.csv')
        with (self.root/'out.csv').open(encoding='utf-8-sig',newline='') as stream:
            reader=csv.DictReader(stream);self.assertEqual(reader.fieldnames,HEADERS);rows=list(reader)
        self.assertEqual(report['house_product_rows'],2)
        self.assertEqual(rows[1]['Адрес дома'],'House B')
        self.assertEqual([rows[1][k] for k in HEADERS[3:6]],['','110','150'])
        self.assertEqual(rows[1]['Описание'],'Site description');self.assertEqual(rows[1]['Белки'],'0')
        self.assertEqual(before,(self.snapshot/'snapshot.sqlite').read_bytes())
    def test_identity_is_strict_and_never_global_sku_only(self):
        for field,value in [('name','Other'),('amount','900 г'),('product_id','2')]:
            self.assertNotEqual(identity(self.row),identity(self.row|{field:value}))
    def test_volume_is_not_guessed_to_be_mass(self):
        self.assertEqual(merge_row(self.row|{'amount':'1 л'},None)['amount'],'')
        self.assertEqual(merge_row(self.row|{'amount':'1 л'},{'metadata':{'weight':{'value':'1050 г'}}})['amount'],'1050 г')
    def test_latest_observation_does_not_fill_missing_card_from_older(self):
        old=self.row|{'extra':{'observation_received_at':'2026-01-01'}}
        new=self.row|{'price_card':None,'extra':{'observation_received_at':'2026-02-01'}}
        result=deduplicate([old,new],'');self.assertEqual(len(result),1);self.assertIsNone(result[0]['price_card'])
    def test_partial_house_is_included_without_overwriting_completed_houses(self):
        self.zone('A',self.row)
        (self.snapshot/'progress.json').write_text(json.dumps({'h3_index':'B'}))
        self.db.execute('INSERT INTO pages VALUES (?,?,?,?)',('["B", "cat"]','url','2026-11-02',json.dumps({'rows':[self.row|{'h3_index':'B'}],'next':None})));self.db.commit()
        store=MetadataStore(self.root/'meta.sqlite');seed=store.seed(self.snapshot);store.close()
        self.assertEqual(seed['houses'],2)
    def test_house_mismatch_is_an_error(self):
        self.zone('A',self.row|{'h3_index':'B'})
        with self.assertRaises(ValueError):export(self.snapshot,self.zones,self.root/'meta.sqlite',self.root/'out.csv')
    def test_manual_guard_blocks_before_any_request(self):
        (self.snapshot/'backoff_status.json').write_text(json.dumps({'manual_required_since_unix':123}))
        with self.assertRaises(core.ProtectionError):check_guards(self.snapshot)

class MetadataReaderTests(unittest.TestCase):
    def setUp(self):
        self.data=copy.deepcopy(core.load_json(Path(__file__).parent/'testdata/current_product_price.json')['data'])
        self.sku='2952307571';self.zone={'address':'House B','h3_index':'B'}
        self.item={'product_id':self.sku,'name':__import__('ozon_har_catalog').widget(self.data,'webProductHeading-')['title'],
                   'amount':'1 л','url':f'https://www.ozon.ru/product/test-{self.sku}/','price':'OLD'}
        self.data['widgetStates']['addressBookBarWeb-fixture']={'customCell':{'actionMode':'common','cells':[{'text':{'text':'House B'}}]}}
        self.data['widgetStates']['webAddToCart-fixture']={'firstButton':{'sku':self.sku,'toCart':{'testInfo':{'automatizationId':'saleblock-add-to-cart-button-express'},'action':{'id':'addToCart','params':{'sku':self.sku}}}}}
        self.data['widgetStates']['paginator-fixture']={'nextPage':f'/product/test-{self.sku}/?layout_container=pdpPage2column&layout_page_index=2'}
        self.client=Mock();self.client.product_page.return_value=self.data
        self.client.entrypoint.return_value={'widgetStates':{'webDescription-test':{'html':'<p>Published description</p>'},
            'webNutritionInfo-test':{'values':[{'label':'белки','value':'0'},{'label':'ккал','value':'50'}]},
            'recommendations-x':{'description':'WRONG','fat':'99'}}}
    def test_published_continuation_and_zero_nutrient_no_price_mutation(self):
        original=copy.deepcopy(self.item);result=read_metadata(self.client,self.zone,self.item)
        self.assertEqual(result['metadata']['description'],'Published description')
        self.assertEqual(result['metadata']['protein'],'0');self.assertNotIn('fat',result['metadata'])
        self.assertEqual(original,self.item);self.assertNotIn('price',result['metadata'])
    def test_wrong_sku_continuation_is_rejected(self):
        self.data['widgetStates']['paginator-fixture']['nextPage']='/product/other-123/?layout_container=pdpPage2column&layout_page_index=2'
        with self.assertRaises(core.ParseError):read_metadata(self.client,self.zone,self.item)
        self.client.entrypoint.assert_not_called()
    def test_wrong_house_is_rejected(self):
        with self.assertRaises(core.ParseError):read_metadata(self.client,self.zone|{'address':'House A'},self.item)
        self.client.entrypoint.assert_not_called()
    def test_same_fresh_sku_heading_conflict_is_isolated_without_copying_metadata(self):
        self.item['name']='Historical catalog name'
        result=read_metadata(self.client,self.zone,self.item)
        self.assertIn('identity_conflict',result)
        self.assertEqual(result['identity_conflict']['saved_name'],'Historical catalog name')
        self.assertTrue(result['identity_conflict']['exact_fresh_sku_confirmed'])
        self.assertNotIn('metadata',result);self.client.entrypoint.assert_not_called()
    def test_different_sku_heading_does_not_pass_the_same_sku_quarantine_check(self):
        self.item['name']='Historical catalog name'
        for key in list(self.data['widgetStates']):
            if key.startswith('webAddToCart-'):del self.data['widgetStates'][key]
        result=read_metadata(self.client,self.zone,self.item)
        self.assertFalse(result['identity_conflict']['exact_fresh_sku_confirmed'])
        self.assertNotIn('metadata',result)
        self.client.entrypoint.assert_not_called()
    def test_changed_primary_sku_is_recorded_without_using_replacement_metadata(self):
        self.item['name']='Historical catalog name'
        for key in list(self.data['widgetStates']):
            if key.startswith('webAddToCart-'):del self.data['widgetStates'][key]
        self.data['widgetStates']['webAddToCart-other']={'firstButton':{'sku':'999','toCart':{
            'testInfo':{'automatizationId':'saleblock-add-to-cart-button-express'},
            'action':{'id':'addToCart','params':{'sku':'999'}}}}}
        result=read_metadata(self.client,self.zone,self.item)
        conflict=result['identity_conflict']
        self.assertFalse(conflict['exact_fresh_sku_confirmed']);self.assertEqual(conflict['primary_cart_skus'],['999'])
        self.assertEqual(conflict['expected_sku'],self.sku);self.assertNotIn('metadata',result)
        self.client.entrypoint.assert_not_called()
    def test_unconfirmed_sku_with_matching_heading_remains_unavailable_at_this_house(self):
        for key in list(self.data['widgetStates']):
            if key.startswith('webAddToCart-'):del self.data['widgetStates'][key]
        result=read_metadata(self.client,self.zone,self.item)
        self.assertEqual(result['unavailable'],'no_current_fresh_offer')
        self.assertNotIn('metadata',result);self.client.entrypoint.assert_not_called()
    def test_protection_and_timeout_propagate_instead_of_marking_product_missing(self):
        for error in (core.ProtectionError('CAPTCHA'),core.ParseError('Browser request failed or timed out; no automatic retry')):
            self.client.product_page.side_effect=error
            with self.assertRaises(type(error)):read_metadata(self.client,self.zone,self.item)
    def test_only_explicit_product404_is_unavailable(self):
        self.client.product_page.side_effect=core.ParseError('HTTP 404; no zone marked complete')
        self.assertEqual(read_metadata(self.client,self.zone,self.item),{'unavailable':'product_http404'})
    def test_durable_resume_does_not_repeat_checked_missing_fields(self):
        entry={'sku':'1','metadata':{},'sources':[{'h3':'A','row':self.item},{'h3':'B','row':self.item}],
               'status':'pending','attempts':[],'checked_at':None}
        first=apply_result(entry,'B',{'unavailable':'product_http404'})
        self.assertEqual(pick_sources({'x':first},'B')[0][0],'A')
        final=apply_result(first,'A',{'metadata':{'protein':'0'},'observed_at':'now','source_url':'url','missing_on_site':['description']})
        self.assertEqual(pick_sources({'x':final},'A'),[])
        self.assertEqual(final['metadata']['protein']['value'],'0')
    def test_exhaust_known_houses_without_identical_infinite_retries(self):
        entry={'sku':'1','metadata':{},'sources':[{'h3':'A','row':self.item}],
               'status':'pending','attempts':[],'checked_at':None}
        final=apply_result(entry,'A',{'unavailable':'product_http404'})
        self.assertEqual(final['status'],'unavailable_at_known_houses');self.assertEqual(pick_sources({'x':final},'A'),[])
    def test_heading_conflict_preserves_known_fields_and_is_not_repeated(self):
        entry={'sku':'1','metadata':{'description':{'value':'Previously verified','at':'old'}},
               'sources':[{'h3':'A','row':self.item}], 'status':'pending','attempts':[],'checked_at':None}
        conflict={'saved_name':'Old','actual_name':'New','expected_sku':'1','exact_fresh_sku_confirmed':True}
        final=apply_result(entry,'A',{'identity_conflict':conflict})
        self.assertEqual(final['status'],'identity_conflict')
        self.assertEqual(final['metadata'],entry['metadata']);self.assertIsNone(final['checked_at'])
        self.assertEqual(final['attempts'][0]['identity_conflict'],conflict)
        self.assertEqual(pick_sources({'x':final},'A'),[])

if __name__=='__main__':unittest.main()
