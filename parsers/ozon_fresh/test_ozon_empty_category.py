"""Actual Fresh empty-state/cursor response, with strict negative regressions."""
import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs,unquote,urlsplit
import ozon_fresh_fast as core
import ozon_har_catalog as tiles
from ozon_live_parser import full_config
from test_ozon_catalog_index import CAT,card,page,enriched

FIXTURE=core.load_json(Path(__file__).parent/'testdata/actual_empty_fresh_category.json')


class EmptyCategoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=copy.deepcopy(FIXTURE);self.data=self.fixture['response']
        self.category=self.fixture['category'];self.url=self.category['url']
        self.schema=full_config([self.category])['catalog_schema']
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.temp.cleanup)
        self.state=core.State(Path(self.temp.name)/'state.sqlite','fixture');self.addCleanup(self.state.db.close)
        self.context={'verified_address':tiles.address_title(self.data),'verified_coordinates':{
            'latitude':self.fixture['zone']['address_lat'],'longitude':self.fixture['zone']['address_lon']}}
    def empty(self,data=None,url=None):
        return core.catalog_page(self.data if data is None else data,self.schema,request_url=url or self.url)
    def test_actual_empty_state_preserves_the_server_continuation(self):
        self.assertFalse(self.fixture['synthetic'])
        items,cursor=self.empty()
        self.assertEqual(items,[]);self.assertEqual(cursor,self.fixture['continuation_url'])
        self.assertIsInstance(cursor,str);self.assertTrue(cursor)
    def test_empty_state_requires_fresh_category_and_matching_request(self):
        bad=copy.deepcopy(self.data);bad['pageInfo']['analyticsInfo']['categoryId']=1
        with self.assertRaises(core.ParseError):self.empty(bad)
        with self.assertRaises(core.ParseError):self.empty(url=CAT['url'])
        bad=copy.deepcopy(self.data);bad['pageInfo']['url']=bad['pageInfo']['url'].replace('miniapp=supermarket','miniapp=marketplace')
        with self.assertRaises(core.ParseError):self.empty(bad)
        bad=copy.deepcopy(self.data)
        next(v for k,v in bad['widgetStates'].items() if k.startswith('addressBookBarWeb-'))['customCell']['actionMode']='individual'
        with self.assertRaises(core.ParseError):self.empty(bad)
    def test_unknown_or_ambiguous_empty_markers_are_not_accepted(self):
        bad=copy.deepcopy(self.data)
        next(n for n in bad['layout'] if n['component']=='statusWidget')['widgetTrackingInfo']['name']='recommendations.empty'
        with self.assertRaises(core.ParseError):self.empty(bad)
        bad=copy.deepcopy(self.data);node=copy.deepcopy(next(n for n in bad['layout'] if n['component']=='statusWidget'))
        node['stateId']='second-empty';bad['layout'].append(node)
        bad['widgetStates']['second-empty']=copy.deepcopy(next(v for k,v in bad['widgetStates'].items() if k.startswith('statusWidget-')))
        with self.assertRaises(core.ParseError):self.empty(bad)
    def test_recommendations_never_replace_primary_results(self):
        bad=copy.deepcopy(self.data);bad['layout'].append({'component':'tileGridDesktop','stateId':'shelf','widgetTrackingInfo':{'name':'shelf'}})
        bad['widgetStates']['shelf']={'items':[{'sku':'123'}]}
        with self.assertRaises(core.ParseError):self.empty(bad)
    def test_empty_category_still_requires_explicit_pagination(self):
        bad=copy.deepcopy(self.data);bad.pop('nextPage')
        for k,v in bad['widgetStates'].items():
            if k.startswith('infiniteVirtualPaginator-'):v.pop('nextPage',None)
        with self.assertRaisesRegex(core.ParseError,'Pagination end'):self.empty(bad)
    def test_metadata_only_tail_requires_parent_and_matching_house_and_category(self):
        data=self.fixture['tail'];cursor=self.fixture['continuation_url'];point=self.context['verified_coordinates']
        self.assertFalse(tiles.empty_catalog_tail(data,cursor))
        self.assertTrue(tiles.empty_catalog_tail(data,cursor,True,point,self.url))
        self.assertFalse(tiles.empty_catalog_tail(data,cursor,True,point,CAT['url']))
        self.assertFalse(tiles.empty_catalog_tail(data,cursor,True,{'latitude':55,'longitude':37},self.url))
        self.assertFalse(tiles.empty_catalog_tail(data,cursor,True,None,self.url))
        bad=copy.deepcopy(data);bad['layoutTrackingInfo']['categoryId']=1
        self.assertFalse(tiles.empty_catalog_tail(bad,cursor,True,point,self.url))
        bad=copy.deepcopy(data);bad['widgetStates']['shelf']={'items':[{'sku':'999'}]}
        self.assertFalse(tiles.empty_catalog_tail(bad,cursor,True,point,self.url))
        bad=copy.deepcopy(data);bad['pageInfo']['url']=bad['pageInfo']['url'].replace('non_found=1','non_found=0')
        self.assertFalse(tiles.empty_catalog_tail(bad,cursor,True,point,self.url))
    def response(self,request):
        url=unquote(request['url'])
        if self.category['category_id'] in url:
            return copy.deepcopy(self.fixture['tail'] if 'page=2' in url else self.data)
        return page([card('1')])
    def test_collection_follows_empty_cursor_and_caches_verified_parent(self):
        client=Mock();client.request.side_effect=self.response
        _,rows=core.collect_catalog(client,full_config([self.category,CAT]),self.context,self.state,'A',enrich_product=enriched)
        self.assertEqual([r['product_id'] for r in rows],['1'])
        self.assertEqual(client.request.call_count,3)
        key=json.dumps(['A',self.category['category_id']])
        first=self.state.get_page(key,self.url)[1]
        self.assertTrue(first['confirmed_empty_primary']);self.assertEqual(first['next'],self.fixture['continuation_url'])
        self.assertEqual(self.state.get_page(key,core.public_url(self.fixture['continuation_url']))[1]['next'],None)
    def test_resume_reuses_empty_parent_proof_and_still_fetches_cursor(self):
        key=json.dumps(['A',self.category['category_id']])
        self.state.put_page(key,self.url,core.now(),[],self.fixture['continuation_url'],confirmed_empty_primary=True)
        client=Mock();client.request.side_effect=self.response
        _,rows=core.collect_catalog(client,full_config([self.category,CAT]),self.context,self.state,'A',enrich_product=enriched)
        self.assertEqual(len(rows),1);self.assertEqual(client.request.call_count,2)
    def test_wrong_house_is_rejected_before_accepting_empty_results(self):
        client=Mock();client.request.return_value=self.data
        with self.assertRaisesRegex(core.ParseError,'address differs'):
            core.collect_catalog(client,full_config([self.category]),dict(self.context,verified_address='Другой адрес'),self.state,'A')
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM pages').fetchone()[0],0)
    def test_unverified_empty_tail_is_saved_and_does_not_complete_category(self):
        client=Mock()
        def response(request):
            data=self.response(request)
            if 'page=2' in unquote(request['url']):data.pop('layoutTrackingInfo')
            return data
        client.request.side_effect=response
        with self.assertRaises(core.ParseError):
            core.collect_catalog(client,full_config([self.category]),self.context,self.state,'A')
        key=json.dumps(['A',self.category['category_id']])
        self.assertEqual(self.state.get_page(key,self.url)[1]['next'],self.fixture['continuation_url'])
        self.assertIsNone(self.state.get_page(key,core.public_url(self.fixture['continuation_url'])))
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM parse_failures').fetchone()[0],1)
        self.assertFalse(self.state.done('A'))


class SeparatorContinuationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=core.load_json(Path(__file__).parent/'testdata/actual_separator_continuation_20261028.json')
        self.data=copy.deepcopy(self.fixture['response']);self.url=self.fixture['request_url']
        self.parent=(self.fixture['previous_url'],self.fixture['previous_next'])
        self.category=dict(self.fixture['category'],url=self.parent[0])
        zone=self.fixture['zone']
        self.context={'verified_address':zone['address'],'verified_coordinates':{
            'latitude':float(zone['address_lat']),'longitude':float(zone['address_lon'])}}
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.temp.cleanup)
        self.state=core.State(Path(self.temp.name)/'state.sqlite','fixture');self.addCleanup(self.state.db.close)
        self.key=json.dumps(['A',self.category['category_id']])
    def recognized(self,data=None,**changes):
        args=dict(previous_page=self.parent,expected_coordinates=self.context['verified_coordinates'],
                  expected_category_url=self.category['url'],expected_address=self.context['verified_address'])
        args.update(changes)
        return tiles.empty_separator_continuation(self.data if data is None else data,self.url,**args)
    def tail(self):
        # Synthetic next response exercises the existing strict metadata-tail contract.
        data=copy.deepcopy(self.data);data.pop('layout');data['widgetStates']={}
        data['pageInfo']['url']=data.pop('nextPage')
        tracking=tiles.decode(data['layoutTrackingInfo'])
        tracking['layoutPageIndex']=int(parse_qs(urlsplit(data['pageInfo']['url']).query)['layout_page_index'][0])
        data['layoutTrackingInfo']=tracking
        return data
    def seed_parent(self):
        self.state.put_page(self.key,self.parent[0],core.now(),[self.fixture['previous_row']],self.url)
    def collect(self,client,**kwargs):
        return core.collect_catalog(client,full_config([self.category]),self.context,self.state,'A',**kwargs)
    def test_saved_response_reproduces_old_error_and_is_not_an_end(self):
        self.assertFalse(self.fixture['synthetic'])
        with self.assertRaisesRegex(core.ParseError,'Primary search results'):
            core.catalog_page(self.data,full_config([self.category])['catalog_schema'],request_url=self.url)
        self.assertTrue(self.recognized())
        self.assertTrue(self.data['nextPage']);self.assertIn('sold_out_page=1',self.data['nextPage'])
    def test_requires_exact_published_parent_cursor_and_category(self):
        for parent in [None,(self.parent[0],self.url+'&wrong=1'),
                       (self.parent[0].replace('page=54','page=53'),self.url),
                       (CAT['url'],self.url)]:
            with self.subTest(parent=parent):self.assertFalse(self.recognized(previous_page=parent))
        self.assertFalse(self.recognized(expected_category_url=CAT['url']))
        self.assertFalse(self.recognized(expected_category_url=self.category['url'].replace('supermarket','marketplace')))
    def test_house_number_corpus_and_coordinates_remain_mandatory(self):
        for address in ['Другой дом',self.context['verified_address'].replace('2 к1','2 к2'),None]:
            with self.subTest(address=address):self.assertFalse(self.recognized(expected_address=address))
        for point in [None,{},dict(latitude=55,longitude=37)]:
            with self.subTest(point=point):self.assertFalse(self.recognized(expected_coordinates=point))
        bad=copy.deepcopy(self.data);bad['location']['current']['fullName']=bad['location']['current']['fullName'].replace('2 корпус 1','2 корпус 2')
        self.assertFalse(self.recognized(bad))
    def test_recommendations_and_unreferenced_states_are_not_empty_results(self):
        for component in ['tileGridDesktop','statusWidget','unknown']:
            bad=copy.deepcopy(self.data);bad['layout'].append({'component':component,'stateId':'shelf','widgetTrackingInfo':{'name':'shelf'}})
            bad['widgetStates']['shelf']={'items':[{'sku':123}]}
            with self.subTest(component=component):self.assertFalse(self.recognized(bad))
        bad=copy.deepcopy(self.data);bad['widgetStates']['hidden']={'items':[{'sku':123}]}
        self.assertFalse(self.recognized(bad))
        bad=copy.deepcopy(self.data);next(iter(bad['widgetStates'].values()))['items']=[]
        self.assertFalse(self.recognized(bad))
    def test_metadata_and_exact_forward_cursor_are_required(self):
        bad_cases=[]
        for cursor in [None,'',self.data['nextPage'].replace('layout_page_index=56','layout_page_index=57'),
                       self.data['nextPage'].replace('non_found=1','non_found=0')]:
            bad=copy.deepcopy(self.data);bad['nextPage']=cursor;bad_cases.append(bad)
        bad=copy.deepcopy(self.data);bad.pop('nextPage');bad_cases.append(bad)
        bad=copy.deepcopy(self.data);bad['pageInfo']['analyticsInfo']['categoryId']=1;bad_cases.append(bad)
        bad=copy.deepcopy(self.data);bad['pageInfo']['url']=bad['pageInfo']['url'].replace('page=55','page=54');bad_cases.append(bad)
        for field,value in [('categoryId',1),('layoutPageIndex',54),('miniapp','marketplace'),('layoutVersion',999)]:
            bad=copy.deepcopy(self.data);tracking=tiles.decode(bad['layoutTrackingInfo']);tracking[field]=value;bad['layoutTrackingInfo']=tracking;bad_cases.append(bad)
        for index,bad in enumerate(bad_cases):
            with self.subTest(case=index):self.assertFalse(self.recognized(bad))
    def test_live_parent_then_separator_follows_the_real_cursor(self):
        first=page([card('1')]);first['nextPage']=self.url
        client=Mock();client.request.side_effect=[first,self.data,self.tail()]
        _,rows=self.collect(client,enrich_product=enriched)
        self.assertEqual([r['product_id'] for r in rows],['1']);self.assertEqual(client.request.call_count,3)
        checkpoint=self.state.get_page(self.key,self.url)[1]
        self.assertEqual(checkpoint['next'],self.data['nextPage']);self.assertTrue(checkpoint['confirmed_empty_primary'])
        end=self.state.get_page(self.key,core.public_url(self.data['nextPage']))[1]
        self.assertIsNone(end['next'])
    def test_resume_cached_parent_does_not_repeat_product_request(self):
        self.seed_parent();client=Mock();client.request.side_effect=[self.data,self.tail()];fetch=Mock()
        _,rows=self.collect(client,enrich_product=fetch)
        self.assertEqual(len(rows),1);self.assertEqual(client.request.call_count,2);fetch.assert_not_called()
        self.assertEqual(rows[0]['product_id'],self.fixture['previous_row']['product_id'])
    def test_resume_after_separator_preserves_empty_parent_proof(self):
        self.seed_parent()
        self.state.put_page(self.key,self.url,core.now(),[],self.data['nextPage'],confirmed_empty_primary=True)
        client=Mock();client.request.return_value=self.tail()
        _,rows=self.collect(client)
        self.assertEqual(len(rows),1);self.assertEqual(client.request.call_count,1)
    def test_invalid_separator_is_not_checkpointed_or_counted_complete(self):
        self.seed_parent();bad=copy.deepcopy(self.data);bad.pop('nextPage')
        client=Mock();client.request.return_value=bad
        with self.assertRaises(core.ParseError):self.collect(client)
        self.assertIsNone(self.state.get_page(self.key,self.url));self.assertFalse(self.state.done('A'))
        self.assertEqual(self.state.get_page(self.key,self.parent[0])[1]['next'],self.url)
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM parse_failures').fetchone()[0],1)
    def test_next_response_must_still_prove_house_and_category(self):
        self.seed_parent();bad=self.tail();bad['location']['current']['geoCoordinate']['latitude']=55
        client=Mock();client.request.side_effect=[self.data,bad]
        with self.assertRaises(core.ParseError):self.collect(client)
        self.assertEqual(self.state.get_page(self.key,self.url)[1]['next'],self.data['nextPage'])
        self.assertIsNone(self.state.get_page(self.key,core.public_url(self.data['nextPage'])))
        self.assertFalse(self.state.done('A'))


if __name__=='__main__':unittest.main()
