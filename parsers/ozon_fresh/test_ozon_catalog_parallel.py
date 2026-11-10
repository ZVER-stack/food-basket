"""Integration checks: real worker pool, synthetic four-category catalog graph."""
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import ozon_fresh_fast as core
from ozon_price_pool import PricePool
from test_ozon_fresh_fast import CARD, config, page


class CatalogPoolIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.stop=threading.Event()
        self.state=core.State(Path(self.tmp.name)/'state.sqlite','fixture')
        self.addCleanup(self.state.db.close)
        self.cats=[{'category_id':str(i),'name':f'Food{i}','scope':'food',
            'url':core.HOST+f'/category/fixture-{i}/?miniapp=supermarket'} for i in range(4)]
        self.conf=config();self.conf['categories']=self.cats

    def pool(self,wrong_store=False,no_requests=False):
        heads=threading.Barrier(4)
        class Client:
            def __init__(self,**options):
                self.owner=threading.get_ident();self.count=0;self.state={}
                self.deadline=time.monotonic()+60;self.closed=False
            def check(self):pass
            def request(self,spec):
                assert threading.get_ident()==self.owner
                if no_requests:raise AssertionError('Checkpoint replay must not fetch')
                self.count+=1
                path=parse_qs(urlsplit(spec['url']).query)['url'][0]
                parts=urlsplit(path);idx=int(parts.path.split('-')[-1].strip('/'))
                tail='cursor' in parse_qs(parts.query)
                if not tail:heads.wait(timeout=3)
                sku=100+idx*2+int(tail)
                card=dict(CARD,sku=sku,url=f'/product/milk-{sku}/')
                next_url=None if tail else core.HOST+parts.path+'?miniapp=supermarket&cursor=opaque-next'
                data=page([card],next_url);data['warehouse']='B' if wrong_store else 'A'
                return data
            def close(self):
                assert threading.get_ident()==self.owner
                self.closed=True
        pool=PricePool(4,'local','known',3,time.monotonic()+60,self.tmp.name,
            self.stop,client_factory=Client)
        self.addCleanup(pool.close)
        return pool

    def test_four_complete_cursor_chains_checkpoint_and_resume_without_fetch(self):
        pool=self.pool()
        coordinator=type('Coordinator',(),{'check':lambda s:None})()
        _,rows=core.collect_catalog(coordinator,self.conf,{'store_id':'A'},self.state,'H',page_pool=pool)
        self.assertEqual({row['product_id'] for row in rows},{str(i) for i in range(100,108)})
        self.assertEqual(pool.report()['catalog_requests'],8)
        for row in rows:
            self.assertEqual((row['price'],row['price_card'],row['price_regular']),('89.90','85','109'))
            self.assertEqual(len(row['parsed_at']),19)
            self.assertIn('observation_received_at',json.loads(row['extra']))
        pool.close()
        replay=self.pool(no_requests=True)
        _,again=core.collect_catalog(coordinator,self.conf,{'store_id':'A'},self.state,'H',page_pool=replay)
        self.assertEqual(sorted(rows,key=lambda r:r['product_id']),sorted(again,key=lambda r:r['product_id']))
        self.assertEqual(replay.report()['requests'],0)

    def test_wrong_context_prevents_export_and_drains_all_readers(self):
        pool=self.pool(wrong_store=True)
        self.conf['catalog_store_pointer']='/warehouse'
        coordinator=type('Coordinator',(),{'check':lambda s:None})()
        with self.assertRaisesRegex(core.ParseError,'Warehouse changed'):
            core.collect_catalog(coordinator,self.conf,{'store_id':'A'},self.state,'H',page_pool=pool)
        self.assertTrue(self.stop.is_set())
        pool.close()
        self.assertTrue(all(c.closed for c in pool.clients.values()))
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM zones').fetchone()[0],0)


if __name__=='__main__':unittest.main()
