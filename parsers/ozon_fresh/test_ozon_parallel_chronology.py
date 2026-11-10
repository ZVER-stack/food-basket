"""Offline price chronology regressions for interleaved category responses."""
from copy import deepcopy
import unittest

import ozon_fresh_fast as core
from ozon_catalog_index import extra_of, merge_observation


def observation(stamp, inferred=False, card='90', ordinary='100', signature='new'):
    return {'product_id': '123', 'h3_index': 'B', 'name': 'Product', 'amount': '1 л',
            'in_stock': 'true', 'price': ordinary, 'price_regular': '120', 'price_card': card,
            'parsed_at': stamp, 'extra': {
                'ordinary_price_source': 'matching_catalog_cache' if inferred else 'product_webPrice',
                'ordinary_price_verified_for_address': not inferred,
                'ordinary_price_inferred': inferred,
                'ordinary_price_origin': {'h3_index': 'A' if inferred else 'B',
                                          'parsed_at': '2026-10-18 10:00:00'},
                'catalog_quote': {'signature': signature},
                'catalog_quote_observed_at': stamp}}


class ParallelChronologyTests(unittest.TestCase):
    def test_old_prefetched_page_processed_last_cannot_replace_newer_price_group(self):
        new_pdp = observation('2026-10-18 12:02:00', card='95', ordinary='105')
        old_prefetch = observation('2026-10-18 12:01:00', card='90', signature='old')
        for first, second in ((new_pdp,old_prefetch),(old_prefetch,new_pdp)):
            result=merge_observation(first,second)
            self.assertEqual((result['price'],result['price_card']),('105','95'))
            self.assertEqual(result['parsed_at'],'2026-10-18 12:02:00')

    def test_newer_direct_verification_replaces_equal_inferred_offer(self):
        inferred=observation('2026-10-18 12:01:00', inferred=True, signature='old')
        direct=observation('2026-10-18 12:02:00')
        result=merge_observation(inferred,direct)
        extra=extra_of(result)
        self.assertFalse(extra['ordinary_price_inferred'])
        self.assertTrue(extra['ordinary_price_verified_for_address'])
        self.assertEqual(extra['ordinary_price_origin']['h3_index'],'B')
        self.assertEqual(extra['catalog_quote'],{'signature':'new'})
        self.assertTrue(extra['earlier_observations'][0]['price_provenance']['ordinary_price_inferred'])

    def test_equal_timestamp_equal_prices_prefer_verified_in_both_processing_orders(self):
        stamp='2026-10-18 12:01:00'
        inferred=observation(stamp,inferred=True)
        direct=observation(stamp)
        for first,second in ((inferred,direct),(direct,inferred)):
            extra=extra_of(merge_observation(first,second))
            self.assertFalse(extra['ordinary_price_inferred'])
            self.assertTrue(extra['ordinary_price_verified_for_address'])

    def test_same_house_reuse_of_other_house_origin_is_still_inferred(self):
        stamp='2026-10-18 12:01:00'
        inferred=observation(stamp,inferred=True)
        inferred['extra']['ordinary_price_source']='same_house_offer_cache'
        direct=observation(stamp)
        for first,second in ((inferred,direct),(direct,inferred)):
            result=merge_observation(first,second)
            self.assertEqual(extra_of(result)['ordinary_price_origin']['h3_index'],'B')

    def test_newer_inference_retains_flag_and_displaced_direct_history(self):
        old=observation('2026-10-18 12:01:00',signature='old')
        new=observation('2026-10-18 12:02:00',inferred=True)
        result=merge_observation(old,new)
        extra=extra_of(result)
        self.assertTrue(extra['ordinary_price_inferred'])
        self.assertFalse(extra['ordinary_price_verified_for_address'])
        self.assertEqual(extra['ordinary_price_origin']['h3_index'],'A')
        earlier=extra['earlier_observations'][0]['price_provenance']
        self.assertTrue(earlier['ordinary_price_verified_for_address'])
        self.assertEqual(earlier['ordinary_price_origin']['h3_index'],'B')

    def test_equal_timestamp_different_prices_still_raise(self):
        first=observation('2026-10-18 12:01:00')
        second=observation('2026-10-18 12:01:00',ordinary='105')
        with self.assertRaises(core.ParseError):merge_observation(first,second)

    def test_newer_equal_price_signature_replaces_old_without_splicing(self):
        old=observation('2026-10-18 12:01:00',signature='old')
        new=observation('2026-10-18 12:02:00')
        before=deepcopy((old,new))
        result=merge_observation(new,old)
        self.assertEqual(extra_of(result)['catalog_quote'],{'signature':'new'})
        self.assertEqual(extra_of(result)['catalog_quote_observed_at'],new['parsed_at'])
        self.assertEqual((old,new),before)


if __name__=='__main__':unittest.main()
