"""Cross-pass reference safety: no new197-house baseline and no false freshness."""
import json,tempfile,unittest
from pathlib import Path
from ozon_catalog_index import CatalogIndex,extra_of,needs_product
import ozon_fresh_fast as core
import ozon_har_catalog as tiles
from test_ozon_catalog_index import saved,card


class HistoricalReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.old=core.State(self.root/'old.sqlite','fixture');self.addCleanup(self.old.db.close)
        self.new=core.State(self.root/'new.sqlite','fixture');self.addCleanup(self.new.db.close)
        self.old.mark('A','collected_with_gaps',[saved('1')])

    def test_partial_old_snapshot_supplies_same_house_price_as_explicitly_unverified(self):
        index=CatalogIndex(self.new,True,self.root/'old.sqlite')
        item=index.resolve('A',tiles.tile_product(card('1')), {})
        self.assertFalse(needs_product(item))
        extra=extra_of(item)
        self.assertTrue(extra['ordinary_price_inferred'])
        self.assertFalse(extra['ordinary_price_verified_for_address'])
        self.assertEqual(extra['ordinary_price_origin']['h3_index'],'A')
        self.assertEqual(extra['ordinary_price_origin']['snapshot_id'],str((self.root/'old.sqlite').resolve()))
        self.assertEqual(index.stats['historical_cache_hits'],1)
        self.assertFalse(self.new.done('A'));self.assertFalse(self.new.done('B'))
        self.assertEqual(self.new.db.execute('SELECT count(*) FROM pages').fetchone()[0],0)

    def test_strict_mode_uses_metadata_but_never_historical_ordinary_price(self):
        index=CatalogIndex(self.new,False,self.root/'old.sqlite')
        self.assertTrue(needs_product(index.resolve('A',tiles.tile_product(card('1')),{})))
        self.assertEqual(index.stats['historical_cache_hits'],0)

    def test_new_sku_or_changed_current_quote_still_needs_product(self):
        index=CatalogIndex(self.new,True,self.root/'old.sqlite')
        for current in (card('2'),card('1',bank='450')):
            self.assertTrue(needs_product(index.resolve('B',tiles.tile_product(current),{})))

    def test_unfinished_old_house_and_inferred_source_are_not_verified_price_references(self):
        self.old.mark('B','error',[saved('2')])
        self.old.put_page('["C", "123"]','https://www.ozon.ru/category/c/',core.now(),[saved('3')],None)
        inferred=saved('4');extra=extra_of(inferred);extra['ordinary_price_inferred']=True
        inferred['extra']=json.dumps(extra);self.old.mark('D','complete',[inferred])
        index=CatalogIndex(self.new,True,self.root/'old.sqlite')
        for sku in ('2','3','4'):
            self.assertTrue(needs_product(index.resolve('E',tiles.tile_product(card(sku)),{})))

    def test_current_verified_quote_replaces_historical_candidate_without_losing_old_origin(self):
        index=CatalogIndex(self.new,True,self.root/'old.sqlite')
        index.publish('B',[saved('1')])
        item=index.resolve('C',tiles.tile_product(card('1')), {})
        extra=extra_of(item)
        self.assertEqual(extra['ordinary_price_origin']['h3_index'],'B')
        self.assertNotIn('snapshot_id',extra['ordinary_price_origin'])
        self.assertEqual(index.stats['historical_cache_hits'],0)

    def test_wrong_fingerprint_and_wrong_fresh_reference_are_rejected(self):
        wrong=core.State(self.root/'wrong.sqlite','wrong');wrong.db.close()
        with self.assertRaisesRegex(core.ParseError,'fingerprint'):
            CatalogIndex(self.new,True,self.root/'wrong.sqlite')
        row=saved('2');extra=extra_of(row);extra['fresh_action_verified']=False
        row['extra']=json.dumps(extra);self.old.mark('B','complete',[row])
        with self.assertRaisesRegex(core.ParseError,'Fresh'):
            CatalogIndex(self.new,True,self.root/'old.sqlite')

    def test_same_snapshot_is_not_imported_as_old_data(self):
        index=CatalogIndex(self.new,True,self.root/'new.sqlite')
        self.assertEqual(index.stats['imported_reference_houses'],0)

    def test_unverified_historical_price_is_not_promoted_to_a_reference(self):
        row=saved('2');extra=extra_of(row)
        extra.update(ordinary_price_verified_for_address=False,ordinary_price_inferred=False)
        row['extra']=json.dumps(extra);self.old.mark('B','complete',[row])
        index=CatalogIndex(self.new,True,self.root/'old.sqlite')
        self.assertTrue(needs_product(index.resolve('E',tiles.tile_product(card('2')),{})))


if __name__=='__main__':unittest.main()
