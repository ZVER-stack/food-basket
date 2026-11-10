"""Offline differential-crawl scenarios; edited tiles simulate different houses."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import ozon_fresh_fast as core
import ozon_har_catalog as tiles
from ozon_catalog_index import (CatalogIndex, checkpoint_quote, extra_of,
                                merge_observation, needs_product, remember_quote)
from ozon_live_parser import full_config
from ozon_product_prices import product_amount, enrich_prices

FIXTURE = core.load_json(Path(__file__).parent/'testdata/public_tiles.json')
CAT = {'category_id':'123', 'name':'Fixture food',
       'url':'https://www.ozon.ru/category/fixture-123/?miniapp=supermarket'}


def card(sku, bank='399', regular='631'):
    result = copy.deepcopy(FIXTURE['samples']['CARD_PRICE'])
    result['sku'] = int(sku)
    result['action']['link'] = f'/product/fixture-{sku}/'
    result['multiButton']['expressButton']['addToCart']['actionButton']['common']['action']['params']['id'] = str(sku)
    price = next(atom['priceV2'] for atom in result['mainState'] if atom['type']=='priceV2')
    price['price'] = [{'text':bank, 'textStyle':'PRICE'}, {'text':regular, 'textStyle':'ORIGINAL_PRICE'}]
    return result


def page(cards):
    return {'layout':[{'component':'tileGridDesktop', 'stateId':'tileGridDesktop-fixture',
                       'widgetTrackingInfo':{'name':'catalog.searchResultsV2'}}],
            'widgetStates':{'tileGridDesktop-fixture':{'items':cards}}, 'nextPage':None}


def enriched(item):
    result = copy.deepcopy(item)
    result['price'] = str(int(item['price_card'])+20)
    result['extra'].update(ordinary_price_source='product_webPrice',
                          ordinary_price_verified_for_address=True, ordinary_price_inferred=False)
    return result


def saved(sku='1', house='A'):
    item = enriched(remember_quote(tiles.tile_product(card(sku))))
    result = core.card_row(item, full_config([CAT])['card_mapping'], CAT)
    result['parsed_at'] = '2026-10-18 12:00:00'
    return result


class DifferentialCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.state = core.State(Path(self.tmp.name)/'snapshot.sqlite','fixture')
        self.addCleanup(self.state.db.close)

    def test_two_houses_only_new_or_changed_offers_request_product_and_absent_is_removed(self):
        index = CatalogIndex(self.state, allow_price_reuse=True)
        first = Mock(); first.request.return_value = page([card('1'),card('2'),card('3')])
        fetch = Mock(side_effect=enriched)
        _, rows_a = core.collect_catalog(first,full_config([CAT]),{},self.state,'A',
                                        enrich_product=fetch,catalog_index=index)
        self.assertEqual(fetch.call_count,3)
        self.state.mark('A','complete',rows_a)
        # Reconstructing after a restart retains the first complete baseline.
        index = CatalogIndex(self.state, allow_price_reuse=True)
        second = Mock(); second.request.return_value = page([card('1'),card('3',bank='450'),card('4')])
        fetch.reset_mock()
        _, rows_b = core.collect_catalog(second,full_config([CAT]),{},self.state,'B',
                                        enrich_product=fetch,catalog_index=index)
        self.assertEqual(fetch.call_count,2)
        self.assertEqual([c.args[0]['product_id'] for c in fetch.call_args_list],['3','4'])
        self.assertEqual({r['product_id'] for r in rows_b},{'1','3','4'})
        by_id = {r['product_id']:r for r in rows_b}
        self.assertEqual(by_id['3']['price'],'470')
        provenance = extra_of(by_id['1'])
        self.assertTrue(provenance['ordinary_price_inferred'])
        self.assertFalse(provenance['ordinary_price_verified_for_address'])
        self.assertEqual(provenance['ordinary_price_origin']['h3_index'],'A')
        self.assertEqual(index.difference(rows_b),{'new_skus':['4'],'absent_skus':['2'],
            'changed_catalog_quotes':['3'],'baseline_skus':3,'current_skus':3})
        self.assertEqual(index.products['2']['product_id'],'2')

    def test_strict_mode_does_not_reuse_prices_between_addresses(self):
        self.state.mark('A','complete',[saved()])
        index = CatalogIndex(self.state,allow_price_reuse=False)
        item = index.resolve('B',tiles.tile_product(card('1')), {})
        self.assertTrue(needs_product(item))
        self.assertEqual(index.stats['cross_house_cache_hits'],0)

    def test_same_house_unreplayed_page_can_supply_matching_price_without_claiming_completeness(self):
        old=saved('1')
        self.state.put_page('["A", "old-category"]','https://www.ozon.ru/category/old/',core.now(),[old],None)
        index=CatalogIndex(self.state,allow_price_reuse=False)
        item=index.resolve('A',tiles.tile_product(card('1')), {})
        self.assertFalse(needs_product(item))
        self.assertEqual(item['price'],old['price'])
        self.assertEqual(index.stats['same_house_cache_hits'],1)
        self.assertFalse(self.state.done('A'))
        self.assertEqual(index.products,{})
        self.assertEqual(index.reference,{})

    def test_unfinished_other_house_checkpoint_cannot_supply_price(self):
        self.state.put_page('["A", "123"]',CAT['url'],core.now(),[saved('1')],None)
        index=CatalogIndex(self.state,allow_price_reuse=True)
        item=index.resolve('B',tiles.tile_product(card('1')), {})
        self.assertTrue(needs_product(item))
        self.assertEqual(index.stats['cross_house_cache_hits'],0)

    def test_changed_visible_quote_does_not_reuse_same_house_checkpoint(self):
        self.state.put_page('["A", "123"]',CAT['url'],core.now(),[saved('1')],None)
        index=CatalogIndex(self.state,allow_price_reuse=True)
        item=index.resolve('A',tiles.tile_product(card('1',bank='450')), {})
        self.assertTrue(needs_product(item))

    def test_same_house_checkpoint_retains_literal_amount_missing_from_current_tile(self):
        old=saved('1');old['name']='Набор';old['amount']='2 шт'
        extra=extra_of(old)
        extra['catalog_quote'].update(name='набор',amount=None)
        old['extra']=json.dumps(extra)
        self.state.put_page('["A", "123"]',CAT['url'],core.now(),[old],None)
        item=tiles.tile_product(card('1'));item.update(name='Набор',amount=None)
        resolved=CatalogIndex(self.state).resolve('A',item,{})
        self.assertEqual(resolved['amount'],'2 шт')
        self.assertFalse(needs_product(resolved))
        self.assertEqual(extra_of(resolved)['amount_origin']['source'],'same_SKU_same_house_checkpoint')

    def test_unchanged_complete_catalog_uses_category_page_without_product_requests(self):
        self.state.mark('A','complete',[saved(str(sku)) for sku in range(1,6)])
        index=CatalogIndex(self.state,allow_price_reuse=True)
        client=Mock();client.request.return_value=page([card(str(sku)) for sku in range(1,6)])
        fetch=Mock(side_effect=enriched)
        _, rows=core.collect_catalog(client,full_config([CAT]),{},self.state,'B',
            enrich_product=fetch,catalog_index=index)
        fetch.assert_not_called()
        self.assertEqual(client.request.call_count,1)
        self.assertEqual(len(rows),5)
        self.assertTrue(all(extra_of(row)['ordinary_price_inferred'] for row in rows))

    def test_changed_duplicate_across_pages_updates_price_and_survives_checkpoint_replay(self):
        first=page([card('1')]);first['nextPage']=CAT['url']+'&page=2'
        second=page([card('1',bank='450')])
        client=Mock();client.request.side_effect=[first,second]
        stamps=iter(f'2026-10-18 12:00:{second:02}' for second in range(60))
        with patch.object(core,'now',side_effect=lambda:next(stamps)):
            _, rows=core.collect_catalog(client,full_config([CAT]),{},self.state,'A',
                enrich_product=enriched,catalog_index=CatalogIndex(self.state))
        self.assertEqual(rows[0]['price'],'470')
        self.assertEqual(extra_of(rows[0])['earlier_observations'][0]['price'],'419')
        resumed=Mock()
        _, replay=core.collect_catalog(resumed,full_config([CAT]),{},self.state,'A',
            enrich_product=enriched,catalog_index=CatalogIndex(self.state))
        resumed.request.assert_not_called()
        self.assertEqual(replay[0]['price'],'470')

    def test_partial_pages_cannot_seed_completed_reference(self):
        self.state.put_page('["A", "123"]',CAT['url'],core.now(),[saved()],None)
        index = CatalogIndex(self.state,allow_price_reuse=True)
        self.assertFalse(index.products)
        self.assertTrue(needs_product(index.resolve('B',tiles.tile_product(card('1')),{})))

    def test_same_name_different_sku_never_reuses_price(self):
        self.state.mark('A','complete',[saved('1')])
        index = CatalogIndex(self.state,allow_price_reuse=True)
        self.assertTrue(needs_product(index.resolve('B',tiles.tile_product(card('2')),{})))

    def test_changed_list_price_alone_invalidates_cache(self):
        self.state.mark('A','complete',[saved()])
        index = CatalogIndex(self.state,allow_price_reuse=True)
        item = tiles.tile_product(card('1',regular='700'))
        self.assertTrue(needs_product(index.resolve('B',item,{})))

    def test_missing_one_visible_price_cannot_authorize_cross_house_reuse(self):
        row = saved()
        extra = extra_of(row);extra['catalog_quote']['price_regular']=None
        row['extra']=json.dumps(extra)
        self.state.mark('A','complete',[row])
        index = CatalogIndex(self.state,allow_price_reuse=True)
        item = tiles.tile_product(card('1'));item['price_regular']=None
        self.assertTrue(needs_product(index.resolve('B',item,{})))

    def test_visible_destination_card_and_list_prices_remain_current(self):
        row=saved();row.update(price_card=None,price_regular='650')
        self.state.mark('A','complete',[row])
        index=CatalogIndex(self.state,allow_price_reuse=True)
        result=index.resolve('B',tiles.tile_product(card('1')), {})
        self.assertFalse(needs_product(result))
        self.assertEqual((result['price'],result['price_regular'],result['price_card']),('419','631','399'))
        self.assertEqual(result['extra']['inferred_price_fields'],['price'])

    def test_cached_ordinary_price_above_destination_list_price_requires_product_check(self):
        row=saved();row.update(price='650',price_regular='700')
        self.state.mark('A','complete',[row])
        index=CatalogIndex(self.state,allow_price_reuse=True)
        self.assertTrue(needs_product(index.resolve('B',tiles.tile_product(card('1')), {})))

    def test_provenance_stays_at_verified_origin_after_same_house_reuse(self):
        self.state.mark('A','complete',[saved()])
        index = CatalogIndex(self.state,allow_price_reuse=True)
        item = tiles.tile_product(card('1'))
        first = index.resolve('B',item,{})
        row = core.card_row(first,full_config([CAT])['card_mapping'],CAT)
        row['parsed_at']='2026-10-18 12:01:00'
        second = index.resolve('B',item,{'1':row})
        self.assertEqual(second['extra']['ordinary_price_origin']['h3_index'],'A')
        self.assertTrue(second['extra']['ordinary_price_inferred'])
        index.publish('B',[row])
        third = index.resolve('C',item,{})
        self.assertEqual(third['extra']['ordinary_price_origin']['h3_index'],'A')

    def test_weighted_quote_reuses_literal_pack_price_without_mass_arithmetic(self):
        raw = tiles.tile_product(core.load_json(Path(__file__).parent/'testdata/unit_price_tile.json')['card'])
        item = remember_quote(raw)
        item.update(price='236',price_regular='269',price_card=None,unit_price='157.33',unit_price_basis='1кг')
        row = core.card_row(item,full_config([CAT])['card_mapping'],CAT)
        row['parsed_at']='2026-10-18 12:00:00'
        self.state.mark('A','complete',[row])
        index = CatalogIndex(self.state,allow_price_reuse=True)
        result = index.resolve('B',raw,{})
        self.assertFalse(needs_product(result))
        self.assertEqual(result['price'],'236')
        self.assertEqual(result['amount'],'1-1.50 кг')
        self.assertEqual(result['unit_price'],'157.33')

    def test_later_duplicate_updates_price_group_and_preserves_old_observation(self):
        first=saved();second=copy.deepcopy(first)
        second.update(price='450',price_regular='650',price_card='430',parsed_at='2026-10-18 12:10:00')
        result=merge_observation(first,second)
        self.assertEqual((result['price'],result['price_regular'],result['price_card']),('450','650','430'))
        self.assertEqual(extra_of(result)['earlier_observations'][0]['price'],first['price'])
        # Category replay order must not reverse a more recent observation.
        self.assertEqual(merge_observation(second,first)['price'],'450')

    def test_conflicting_simultaneous_quotes_are_not_silently_chosen(self):
        first=saved();second=dict(first,price='450')
        with self.assertRaises(core.ParseError):merge_observation(first,second)

    def test_batch_path_skips_cached_product_but_fetches_new_item(self):
        self.state.mark('A','complete',[saved()])
        index=CatalogIndex(self.state,allow_price_reuse=True)
        client=Mock();client.request.return_value=page([card('1'),card('2')])
        batch=Mock(side_effect=lambda items:[enriched(item) for item in items])
        _, rows=core.collect_catalog(client,full_config([CAT]),{},self.state,'B',
            enrich_products=batch,catalog_index=index)
        self.assertEqual(len(rows),2)
        self.assertEqual([item['product_id'] for item in batch.call_args.args[0]],['2'])

    def test_old_checkpoint_recovery_retains_explicit_price_and_raw_quote(self):
        row=saved();extra=extra_of(row);del extra['catalog_quote'];row['extra']=json.dumps(extra)
        recovered=checkpoint_quote(row)
        self.assertEqual(recovered['price'],row['price'])
        self.assertIsNone(extra_of(recovered)['catalog_quote']['price'])
        self.state.mark('A','complete',[row])
        index=CatalogIndex(self.state,allow_price_reuse=True)
        self.assertFalse(needs_product(index.resolve('B',tiles.tile_product(card('1')),{})))

    def test_actual_missing_tile_amount_is_read_from_explicit_product_weight(self):
        raw=core.load_json(Path(__file__).parent/'testdata/failed_amount_tile.json')
        product=core.load_json(Path(__file__).parent/'testdata/amount_product.json')
        self.assertFalse(raw['synthetic']);self.assertFalse(product['synthetic'])
        self.assertIsNone(tiles.tile_product(raw['card'])['amount'])
        amount,origin=product_amount(product['data'])
        self.assertEqual(amount,'35 г')
        self.assertEqual(origin['displayed_value'],'35')
        self.assertEqual(origin['title'],'Вес товара, г')

    def test_primary_characteristics_only_and_no_invented_default_amount(self):
        product=core.load_json(Path(__file__).parent/'testdata/amount_product.json')['data']
        data={'widgetStates':{'recommendations-fixture':next(v for k,v in product['widgetStates'].items() if k.startswith('webShortCharacteristics-'))}}
        with self.assertRaises(core.ParseError):product_amount(data)

    def test_known_sku_reuses_static_amount_even_when_price_changes(self):
        raw=tiles.tile_product(core.load_json(Path(__file__).parent/'testdata/failed_amount_tile.json')['card'])
        prepared=remember_quote(raw);prepared.update(price='69',amount='35 г')
        row=core.card_row(prepared,full_config([CAT])['card_mapping'],CAT)
        row['parsed_at']='2026-10-18 12:00:00'
        self.state.mark('A','complete',[row])
        index=CatalogIndex(self.state,allow_price_reuse=True)
        raw['price_card']='67'
        result=index.resolve('B',raw,{})
        self.assertEqual(result['amount'],'35 г')
        self.assertTrue(needs_product(result))
        self.assertEqual(result['extra']['amount_origin']['source'],'same_SKU_metadata_reference')

    def test_missing_required_field_keeps_failed_card_for_offline_diagnosis(self):
        raw=core.load_json(Path(__file__).parent/'testdata/failed_amount_tile.json')['card']
        client=Mock();client.request.return_value=page([raw])
        with self.assertRaises(core.ParseError):
            core.collect_catalog(client,full_config([CAT]),{},self.state,'A',
                enrich_product=enriched,catalog_index=CatalogIndex(self.state))
        record=self.state.db.execute('SELECT product_id,payload FROM parse_failures').fetchone()
        self.assertEqual(record[0],'3250518930')
        self.assertEqual(str(json.loads(record[1])['raw_card']['sku']),'3250518930')

    def test_real_laundry_count_words_and_characteristics_are_literal(self):
        fixture=core.load_json(Path(__file__).parent/'testdata/count_word_products.json')
        self.assertFalse(fixture['synthetic'])
        for sample in fixture['samples']:
            with self.subTest(sku=sample['card']['sku']):
                item=tiles.tile_product(sample['card'])
                self.assertEqual(item['amount'],'100 штук')
                amount,origin=product_amount(sample['data'])
                self.assertEqual(amount,'100 шт')
                self.assertEqual(origin['displayed_value'],'100')
                client=Mock();client.product_page.return_value=sample['data']
                result=enrich_prices(client,{'address':'Москва, Бакунинская улица, 60/2'},item)
                self.assertEqual(result['amount'],'100 штук')
                expected='1252' if item['product_id']=='2054381065' else '1319'
                self.assertEqual(result['price'],expected)

    def test_count_word_endings_preserve_quantity_without_price_math(self):
        for quantity in ('100 штук','1 штука','2 штуки'):
            self.assertEqual(core.AMOUNT.findall('Средство '+quantity),[quantity])
            self.assertEqual(tiles.full_amount(quantity),quantity)

    def test_product_parse_error_preserves_primary_response_locally(self):
        sample=core.load_json(Path(__file__).parent/'testdata/count_word_products.json')['samples'][0]
        item=tiles.tile_product(sample['card']);item['name']='Wrong product'
        client=Mock();client.state_path=Path(self.tmp.name)/'reader.json'
        client.product_page.return_value=sample['data']
        with self.assertRaises(core.ParseError):
            enrich_prices(client,{'address':'Москва, Бакунинская улица, 60/2'},item)
        saved=core.load_json(Path(self.tmp.name)/'parse_failures'/f"product_{item['product_id']}.json")
        self.assertIn('heading',saved['error'])
        self.assertTrue(any(k.startswith('webProductHeading-') for k in saved['data']['widgetStates']))
        client.product_page.assert_called_once()


if __name__=='__main__':unittest.main()
