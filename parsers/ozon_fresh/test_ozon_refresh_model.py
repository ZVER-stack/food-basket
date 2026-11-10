"""Selective refresh scenarios using minimized real public catalog tiles."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

import ozon_fresh_fast as core
import ozon_har_catalog as tiles
from ozon_catalog_index import extra_of, remember_quote
from ozon_refresh_model import RefreshAccumulator

FIXTURE=core.load_json(Path(__file__).parent/'testdata/public_tiles.json')
CAT={'category_id':'123','name':'Fixture food'}
ZONE={'h3_index':'A','address':'Москва, Бакунинская улица, 60с2'}
STAMP='2026-10-18 14:00:00'


def item(style='CARD_PRICE'):
    return tiles.tile_product(deepcopy(FIXTURE['samples'][style]))


def explicit_same_sku():
    raw=deepcopy(FIXTURE['samples']['CARD_PRICE'])
    price=next(atom['priceV2'] for atom in raw['mainState'] if atom['type']=='priceV2')
    price['priceStyle']['styleType']='ACTUAL_PRICE'
    price['price']=[{'text':'419','textStyle':'PRICE'}, {'text':'631','textStyle':'ORIGINAL_PRICE'}]
    return tiles.tile_product(raw)


def baseline(decoded=None):
    prepared=remember_quote(decoded or item())
    prepared['price']='419'
    prepared['price_regular']='631'
    prepared['price_card']='399'
    prepared['extra'].update(ordinary_price_source='product_webPrice',
        ordinary_price_verified_for_address=True,ordinary_price_inferred=False,
        ordinary_price_origin={'h3_index':'A','parsed_at':'2026-10-18 12:00:00',
                               'source':'product_webPrice','snapshot_id':'original'})
    mapping={key:'/'+key for key in core.COLUMNS if key not in ('service','h3_index','parsed_at','category')}
    row=core.card_row(prepared,mapping,CAT)
    row.update(service='ozon_fresh',h3_index='A',parsed_at='2026-10-18 12:00:00')
    return row


class SelectiveRefreshTests(unittest.TestCase):
    def test_same_house_historical_price_is_inferred_and_original_origin_is_preserved(self):
        model=RefreshAccumulator(ZONE,[baseline()],'baseline-pass')
        result=model.observe(CAT,item(),STAMP)
        self.assertFalse(result['needs_product'])
        row=next(iter(model.observations.values()));extra=extra_of(row)
        self.assertTrue(extra['ordinary_price_inferred'])
        self.assertFalse(extra['ordinary_price_verified_for_address'])
        self.assertEqual(extra['ordinary_price_origin']['snapshot_id'],'original')
        self.assertNotIn('price',extra['verified_fields'])
        self.assertEqual(list(row),core.COLUMNS)
        self.assertTrue(all(row[key] is not None for key in core.REQUIRED))

    def test_changed_visible_quote_requires_product_without_accepting_old_price(self):
        model=RefreshAccumulator(ZONE,[baseline()],'baseline')
        changed=item();changed['price_card']='450'
        result=model.observe(CAT,changed,STAMP)
        self.assertTrue(result['needs_product'])
        self.assertFalse(model.observations)

    def test_explicit_ordinary_price_is_accepted_without_product(self):
        model=RefreshAccumulator(ZONE,[],'empty')
        decoded=item('ACTUAL_PRICE')
        result=model.observe(CAT,decoded,STAMP)
        self.assertFalse(result['needs_product'])
        row=model.observations[decoded['product_id']]
        extra=extra_of(row)
        self.assertIn('price',extra['verified_fields'])
        self.assertFalse(extra['full_zone_complete'])
        self.assertFalse(extra['catalog_fully_checked'])

    def test_missing_amount_uses_only_same_sku_name_literal_metadata(self):
        decoded=item('ACTUAL_PRICE')
        old=baseline(decoded);old['amount']='330 гр'
        model=RefreshAccumulator(ZONE,[old],'baseline')
        decoded['amount']=None
        result=model.observe(CAT,decoded,STAMP)
        self.assertFalse(result['needs_product'])
        row=model.observations[decoded['product_id']]
        self.assertEqual(row['amount'],'330 гр')
        extra=extra_of(row)
        self.assertNotIn('amount',extra['verified_fields'])
        self.assertIsNone(extra['catalog_quote']['amount'])

    def test_new_without_ordinary_price_is_deferred_outside_main_rows(self):
        model=RefreshAccumulator(ZONE,[],'empty')
        decoded=item();result=model.observe(CAT,decoded,STAMP)
        self.assertTrue(result['needs_product'])
        model.defer(CAT,result['item'],STAMP,'time budget for product request exhausted')
        self.assertEqual(len(model.pending),1)
        self.assertFalse(model.observations)
        self.assertIn(decoded['product_id'],model.seen)

    def test_unseen_or_pending_baseline_products_are_not_removed(self):
        old=baseline();model=RefreshAccumulator(ZONE,[old],'baseline')
        summary=model.summary()
        self.assertEqual(summary['unchecked_baseline_skus'],[old['product_id']])
        self.assertEqual(summary['removed_confirmed'],0)
        changed=item();changed['price_card']='450'
        result=model.observe(CAT,changed,STAMP)
        model.defer(CAT,result['item'],STAMP,'pending current ordinary price')
        self.assertEqual(model.summary()['unchecked_baseline_skus'],[old['product_id']])

    def test_older_prefetched_duplicate_cannot_overwrite_newer_pdp_quote(self):
        model=RefreshAccumulator(ZONE,[],'empty')
        newer=remember_quote(item());newer.update(price='440',price_regular='650',price_card='420')
        newer['extra'].update(ordinary_price_source='product_webPrice',
            ordinary_price_verified_for_address=True,ordinary_price_inferred=False)
        model.accept(CAT,newer,'2026-10-18 14:02:00')
        older=remember_quote(item());older.update(price='419')
        model.accept(CAT,older,'2026-10-18 14:01:00')
        self.assertEqual(model.observations[newer['product_id']]['price'],'440')
        self.assertEqual(model.observations[newer['product_id']]['parsed_at'],'2026-10-18 14:02:00')

    def test_matching_name_without_matching_sku_cannot_supply_amount(self):
        old=baseline();decoded=item('ACTUAL_PRICE');decoded['product_id']='999'
        decoded['url']='https://www.ozon.ru/product/fixture-999/'
        decoded['amount']=None;decoded['name']='Товар без фасовки'
        old['name']=decoded['name']
        model=RefreshAccumulator(ZONE,[old],'baseline')
        result=model.observe(CAT,decoded,STAMP)
        self.assertTrue(result['needs_product'])
        self.assertIsNone(result['item']['amount'])

    def test_accepting_older_prefetched_observation_does_not_clear_newer_pending_offer(self):
        model=RefreshAccumulator(ZONE,[],'empty')
        pending=remember_quote(item())
        model.defer(CAT,pending,'2026-10-18 14:02:00','newer changed quote not priced yet')
        old=remember_quote(explicit_same_sku())
        model.accept(CAT,old,'2026-10-18 14:01:00')
        self.assertEqual(model.summary()['pending_count'],1)
        self.assertFalse(model.observations)

    def test_microsecond_receipt_preserved_while_csv_timestamp_stays_19_characters(self):
        model=RefreshAccumulator(ZONE,[],'empty')
        receipt='2026-10-18 14:00:00.123456'
        decoded=explicit_same_sku()
        model.observe(CAT,decoded,receipt)
        row=model.observations[decoded['product_id']]
        self.assertEqual(row['parsed_at'],'2026-10-18 14:00:00')
        self.assertEqual(extra_of(row)['observation_received_at'],receipt)
        self.assertEqual(extra_of(row)['catalog_quote_observed_at'],receipt)

    def test_changed_prices_in_same_second_follow_real_microsecond_chronology(self):
        model=RefreshAccumulator(ZONE,[],'empty')
        old=remember_quote(item('ACTUAL_PRICE'))
        new=deepcopy(old);new.update(price='500',price_regular='650',price_card='480')
        model.accept(CAT,new,'2026-10-18 14:00:00.900000')
        model.accept(CAT,old,'2026-10-18 14:00:00.100000')
        row=model.observations[old['product_id']]
        self.assertEqual(row['price'],'500')
        self.assertEqual(extra_of(row)['observation_received_at'],'2026-10-18 14:00:00.900000')

    def test_equivalent_second_and_microsecond_receipts_do_not_invent_ordering(self):
        model=RefreshAccumulator(ZONE,[],'empty')
        old=remember_quote(item('ACTUAL_PRICE'))
        new=deepcopy(old);new.update(price='500',price_regular='650',price_card='480')
        model.accept(CAT,old,'2026-10-18 14:00:00')
        with self.assertRaises(core.ParseError):
            model.accept(CAT,new,'2026-10-18 14:00:00.000000')

    def test_newer_changed_pending_quote_suppresses_previously_valid_current_row(self):
        old=baseline();model=RefreshAccumulator(ZONE,[old],'baseline')
        decoded=explicit_same_sku()
        model.observe(CAT,decoded,'2026-10-18 14:00:00.100000')
        self.assertEqual(len(model.observations),1)
        changed=item();changed['price_card']='450'
        result=model.observe(CAT,changed,'2026-10-18 14:00:00.900000')
        self.assertTrue(result['needs_product'])
        self.assertFalse(model.observations)
        model.defer(CAT,result['item'],'2026-10-18 14:00:00.900000','PDP not checked')
        self.assertEqual(model.summary()['unchecked_baseline_skus'],[old['product_id']])
        self.assertEqual(model.summary()['pending_count'],1)

    def test_newer_product_confirmation_resolves_pending_changed_quote(self):
        model=RefreshAccumulator(ZONE,[],'empty')
        pending=remember_quote(item())
        model.defer(CAT,pending,'2026-10-18 14:00:00.100000','pending')
        verified=deepcopy(pending);verified.update(price='419')
        verified['extra'].update(ordinary_price_source='product_webPrice',
            ordinary_price_verified_for_address=True,ordinary_price_inferred=False)
        model.accept(CAT,verified,'2026-10-18 14:00:00.200000')
        self.assertEqual(len(model.observations),1)
        self.assertFalse(model.pending)


if __name__=='__main__':unittest.main()
