import tempfile
import threading
import time
import unittest
from pathlib import Path
import ozon_fresh_fast as core
from ozon_price_pool import PricePool
from ozon_product_prices import MissingAmountError
from ozon_product_redirect import UnavailableProduct

class FakeClient:
    def __init__(self,**kwargs):
        self.options=kwargs;self.count=0;self.state={};self.deadline=time.monotonic()+kwargs['seconds']
        self.owner=threading.get_ident();self.closed=False
    def close(self):
        assert threading.get_ident()==self.owner
        self.closed=True
    def request(self,spec):
        assert threading.get_ident()==self.owner
        self.count+=1
        return self.request_handler(self,spec) if hasattr(self,'request_handler') else {'echo':spec}

class PoolChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.tmp.cleanup)
        self.event=threading.Event()
    def pool(self,enricher,workers=3,request_handler=None,**options):
        def factory(**kwargs):
            client=FakeClient(**kwargs)
            if request_handler is not None:client.request_handler=request_handler
            return client
        pool=PricePool(workers,'http://127.0.0.1:9222','https://www.ozon.ru/product/test-123/',3,
            time.monotonic()+60,self.tmp.name,self.event,client_factory=factory,enricher=enricher,**options)
        self.addCleanup(pool.close)
        return pool
    def test_parallel_jobs_share_house_and_return_in_input_order_before_address_can_change(self):
        barrier=threading.Barrier(3);seen=[]
        def enrich(client,zone,item):
            seen.append(zone['address']);barrier.wait(timeout=3)
            client.count+=1
            return dict(item,price=str(int(item['product_id'])+100))
        pool=self.pool(enrich)
        items=[{'product_id':str(index)} for index in range(3)]
        rows=pool.enrich({'address':'Дом А'},items)
        self.assertEqual([row['product_id'] for row in rows],['0','1','2'])
        self.assertEqual(seen,['Дом А']*3)
        self.assertEqual(pool.report()['requests'],3)
        pool.close()
        self.assertTrue(all(client.closed for client in pool.clients.values()))
        self.assertTrue(all(client.options['product_mode']=='api' for client in pool.clients.values()))
        self.assertFalse(any(client.options.get('isolated_context') for client in pool.clients.values()))
    def test_protection_stops_pending_and_next_house_without_retry(self):
        def fail(client,zone,item):
            client.count+=1;raise core.ProtectionError('HTTP403')
        pool=self.pool(fail)
        with self.assertRaises(core.ProtectionError):pool.enrich({'address':'Дом А'},[{'product_id':'1'}])
        with self.assertRaises(core.BudgetError):pool.enrich({'address':'Дом Б'},[{'product_id':'2'}])
        self.assertEqual(pool.report()['requests'],1)

    def test_confirmed_unavailable_product_is_quarantined_without_stopping_other_skus(self):
        def enrich(client,zone,item):
            client.count+=1
            if item['product_id']=='1':raise UnavailableProduct('published_same_sku_unavailable_redirect',item)
            return dict(item,price='100')
        pool=self.pool(enrich,quarantine_incomplete=True)
        rows=pool.enrich({'address':'House1'},[{'product_id':'1','price':'90'},{'product_id':'2'}])
        self.assertEqual(rows[0]['_quarantine_reason'],'published_same_sku_unavailable_redirect')
        self.assertEqual(rows[0]['price'],'90')
        self.assertEqual(rows[1]['price'],'100')
        self.assertFalse(self.event.is_set())

    def test_unavailable_product_still_stops_without_explicit_quarantine(self):
        def enrich(client,zone,item):raise UnavailableProduct('unavailable',item)
        pool=self.pool(enrich)
        with self.assertRaises(UnavailableProduct):pool.enrich({'address':'House1'},[{'product_id':'1'}])
        self.assertTrue(self.event.is_set())
    def test_failure_waits_for_already_running_old_house_request(self):
        barrier=threading.Barrier(2);finished=threading.Event()
        def enrich(client,zone,item):
            barrier.wait(timeout=3)
            if item['product_id']=='1':raise core.ParseError('bad price')
            finished.set();return item
        pool=self.pool(enrich)
        with self.assertRaises(core.ParseError):pool.enrich({'address':'Дом А'},[{'product_id':'1'},{'product_id':'2'}])
        self.assertTrue(finished.is_set())
    def test_catalog_requests_run_concurrently_on_their_own_clients(self):
        barrier=threading.Barrier(3);owners=set()
        def request(client,spec):
            owners.add(threading.get_ident());barrier.wait(timeout=3)
            return {'category':spec['category']}
        pool=self.pool(lambda client,zone,item:item,request_handler=request)
        futures=[pool.request_async({'category':index}) for index in range(3)]
        responses=[future.result(timeout=5) for future in futures]
        self.assertEqual([x['data']['category'] for x in responses],[0,1,2])
        self.assertEqual(len(owners),3)
        self.assertEqual(owners,{client.owner for client in pool.clients.values()})
        for response in responses:
            core.datetime.strptime(response['captured_at'],'%Y-%m-%d %H:%M:%S.%f')
        self.assertEqual(pool.report()['catalog_requests'],3)
        self.assertEqual(pool.report()['product_requests'],0)
    def test_four_threads_are_shared_between_catalog_and_product_jobs(self):
        barrier=threading.Barrier(4);owners=set()
        def request(client,spec):
            owners.add(threading.get_ident());barrier.wait(timeout=3)
            return {'page':spec['page']}
        def enrich(client,zone,item):
            self.assertEqual(threading.get_ident(),client.owner)
            owners.add(threading.get_ident());barrier.wait(timeout=3)
            client.count+=1
            return dict(item,price='100')
        gate=object()
        pool=self.pool(enrich,workers=4,request_handler=request,backoff_gate=gate)
        futures=[pool.request_async({'page':index}) for index in range(3)]
        rows=pool.enrich({'address':'Дом А'},[{'product_id':'1'}])
        for future in futures:future.result(timeout=5)
        self.assertEqual(rows[0]['price'],'100')
        self.assertEqual(len(pool.threads),4)
        self.assertEqual(owners,{client.owner for client in pool.clients.values()})
        self.assertEqual(pool.report()['requests'],4)
        self.assertEqual(pool.report()['catalog_requests'],3)
        self.assertEqual(pool.report()['product_requests'],1)
        self.assertTrue(all(client.options['backoff_gate'] is gate for client in pool.clients.values()))
    def test_catalog_error_propagates_and_blocks_next_house(self):
        def request(client,spec):raise core.ProtectionError('HTTP403')
        pool=self.pool(lambda client,zone,item:item,request_handler=request)
        future=pool.request_async({'page':1})
        with self.assertRaises(core.ProtectionError):future.result(timeout=3)
        self.assertTrue(self.event.is_set())
        with self.assertRaises(core.BudgetError):pool.request_async({'page':2})
        with self.assertRaises(core.BudgetError):pool.enrich({'address':'Дом Б'},[{'product_id':'2'}])
        self.assertEqual(pool.report()['catalog_requests'],1)
    def test_missing_amount_only_quarantines_product_jobs(self):
        def enrich(client,zone,item):
            client.count+=1
            raise MissingAmountError('No package amount',item)
        pool=self.pool(enrich,quarantine_incomplete=True)
        rows=pool.enrich({'address':'Дом А'},[{'product_id':'1'}])
        self.assertEqual(rows[0]['_quarantine_reason'],'No package amount')
        self.assertFalse(self.event.is_set())
        def request(client,spec):raise MissingAmountError('Bad catalog format',{'product_id':'2'})
        for client in pool.clients.values():client.request_handler=request
        with self.assertRaises(MissingAmountError):pool.request_async({'page':1}).result(timeout=3)
        self.assertTrue(self.event.is_set())
    def test_close_drains_an_already_running_catalog_request(self):
        started=threading.Event();release=threading.Event()
        def request(client,spec):
            started.set()
            if not release.wait(timeout=3):raise AssertionError('request not released')
            return {'complete':True}
        pool=self.pool(lambda client,zone,item:item,request_handler=request)
        future=pool.request_async({'page':1});self.assertTrue(started.wait(timeout=3))
        closer=threading.Thread(target=pool.close);closer.start()
        self.assertTrue(closer.is_alive())
        release.set();closer.join(timeout=5)
        self.assertFalse(closer.is_alive())
        self.assertTrue(future.result(timeout=3)['data']['complete'])
        self.assertTrue(all(client.closed for client in pool.clients.values()))
    def test_cancelled_queued_catalog_job_does_not_kill_reader_or_send_request(self):
        started=threading.Event();release=threading.Event()
        def request(client,spec):
            if spec['page']==1:
                started.set()
                if not release.wait(timeout=3):raise AssertionError('request not released')
            return {'page':spec['page']}
        pool=self.pool(lambda client,zone,item:item,workers=1,request_handler=request)
        first=pool.request_async({'page':1});self.assertTrue(started.wait(timeout=3))
        cancelled=pool.request_async({'page':2});self.assertTrue(cancelled.cancel())
        final=pool.request_async({'page':3});release.set()
        self.assertEqual(first.result(timeout=3)['data']['page'],1)
        self.assertEqual(final.result(timeout=3)['data']['page'],3)
        self.assertTrue(cancelled.cancelled())
        self.assertFalse(self.event.is_set())
        self.assertEqual(pool.report()['catalog_requests'],2)

if __name__=='__main__':unittest.main()
