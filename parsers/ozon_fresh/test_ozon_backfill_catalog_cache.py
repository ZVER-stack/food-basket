import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import ozon_fresh_fast as core
import ozon_har_catalog as tiles
from ozon_catalog_index import CatalogIndex,extra_of,needs_product
from ozon_house_export import MetadataStore,identity
from test_ozon_catalog_index import saved,card


class BackfillCatalogCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.state=core.State(self.root/'snapshot.sqlite','fixture')
        self.addCleanup(self.state.db.close)
        self.old=saved()
        self.current=tiles.tile_product(card('1'))
        self.store=self.root/'metadata.sqlite'
        db=MetadataStore(self.store)
        entry={'sku':'1','metadata':{
            'description':{'value':'Verified PDP description','at':'2026-11-05 12:00:00','h3':'A','source':'verified_primary_product'},
            'protein':{'value':'8.1999999999999993','at':'2026-11-05 12:00:00','h3':'A','source':'verified_primary_product'},
            'price':{'value':'999999','at':'2026-11-05 12:00:00'},
            'in_stock':{'value':False}},
            'sources':[],'status':'checked_missing_on_site','attempts':[],'checked_at':'2026-11-05 12:00:00'}
        db.save(identity(self.current),entry)
        db.db.execute('INSERT INTO job_meta VALUES (?,?)',('seed',json.dumps({'source':str(self.root.resolve())})))
        db.db.commit();db.close()

    def index(self):
        index=CatalogIndex(self.state,allow_price_reuse=True,metadata_strategy=True,metadata_store=self.store)
        index.publish('A',[self.old])
        return index

    def test_checked_backfill_avoids_card_with_identical_quote_and_preserves_house_offer(self):
        before=hashlib.sha256(self.store.read_bytes()).hexdigest()
        index=self.index()
        current=copy.deepcopy(self.current);current['extra']['delivery_address']='House B'
        result=index.resolve('B',current,{})
        self.assertFalse(needs_product(result))
        self.assertEqual(result['description'],'Verified PDP description')
        self.assertEqual(result['protein'],'8.1999999999999993')
        self.assertEqual(result['extra']['delivery_address'],'House B')
        self.assertEqual(result['in_stock'],current['in_stock'])
        self.assertNotEqual(result['price'],'999999')
        self.assertEqual(result['price_card'],current['price_card'])
        self.assertNotIn('h3_index',result)
        self.assertEqual(index.stats['backfill_card_reads_avoided'],1)
        self.assertEqual(hashlib.sha256(self.store.read_bytes()).hexdigest(),before)

    def test_any_changed_price_still_requires_card(self):
        for field in ('price','price_card','price_regular'):
            with self.subTest(field=field):
                current=copy.deepcopy(self.current);current[field]='999'
                self.assertTrue(needs_product(self.index().resolve('B',current,{})))

    def test_unknown_or_conflicting_identity_does_not_receive_metadata(self):
        for field,value in [('product_id','2'),('name','Another product'),('amount','900 г')]:
            with self.subTest(field=field):
                current=copy.deepcopy(self.current);current[field]=value
                result=self.index().resolve('B',current,{})
                self.assertTrue(needs_product(result))
                self.assertNotIn('backfill_metadata_origin',extra_of(result))

    def test_unavailable_conflicting_pending_rows_are_never_used(self):
        for status in ('identity_conflict','unavailable_at_known_houses','pending'):
            with self.subTest(status=status):
                db=MetadataStore(self.store);db.db.execute('UPDATE products SET status=?',(status,));db.db.commit();db.close()
                result=self.index().resolve('B',self.current,{})
                self.assertTrue(needs_product(result))
                self.assertNotIn('description',result)

    def test_old_quote_cannot_override_more_recent_product_price_change(self):
        index=self.index();newer=copy.deepcopy(self.old)
        newer['extra']=extra_of(newer)
        newer['extra'].update(product_metadata_observed_at='2026-11-06 12:00:00',product_metadata_schema=2)
        newer['extra']['catalog_quote']['price_card']='999'
        index.remember_product('C',newer)
        self.assertTrue(needs_product(index.resolve('B',self.current,{})))

    def test_metadata_without_saved_quote_still_requires_card(self):
        index=CatalogIndex(self.state,allow_price_reuse=True,metadata_strategy=True,metadata_store=self.store)
        result=index.resolve('B',self.current,{})
        self.assertTrue(needs_product(result))
        self.assertEqual(result['description'],'Verified PDP description')

    def test_other_snapshot_store_is_rejected(self):
        db=MetadataStore(self.store);db.db.execute('UPDATE job_meta SET value=?',(json.dumps({'source':str(self.root/'other')}),));db.db.commit();db.close()
        with self.assertRaises(core.ParseError):self.index()


if __name__=='__main__':unittest.main()
