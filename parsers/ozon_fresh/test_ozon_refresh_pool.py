import tempfile
import threading
import time
import unittest
from pathlib import Path

import ozon_fresh_fast as core
from ozon_refresh_pool import SampleReaders


class FakeClient:
    def __init__(self,handler,**options):
        self.options=options;self.handler=handler;self.owner=threading.get_ident()
        self.count=0;self.closed=False;self.deadline=time.monotonic()+options['seconds']
    def request(self,spec):
        assert threading.get_ident()==self.owner
        self.count+=1
        return self.handler(self,spec)
    def close(self):
        assert threading.get_ident()==self.owner
        self.closed=True


class SampleChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.stop=threading.Event();self.gate=object()
    def readers(self,handler,workers=4,deadline=None):
        readers=SampleReaders(workers,'http://127.0.0.1:9222',
            'https://www.ozon.ru/product/test-123/',3,
            deadline if deadline is not None else time.monotonic()+60,
            self.temp.name,self.stop,self.gate,
            client_factory=lambda **options:FakeClient(handler,**options))
        self.addCleanup(readers.close)
        return readers
    def test_four_simultaneous_samples_keep_input_order_and_client_ownership(self):
        barrier=threading.Barrier(4);owners=set()
        def handler(client,spec):
            owners.add(threading.get_ident());barrier.wait(timeout=3)
            return {'sample':spec['sample']}
        readers=self.readers(handler)
        rows=readers.request_many([{'sample':index} for index in range(4)])
        self.assertEqual([row['result']['data']['sample'] for row in rows],list(range(4)))
        self.assertTrue(all(row['error'] is None for row in rows))
        self.assertEqual(owners,{client.owner for client in readers.clients.values()})
        self.assertEqual(len(owners),4)
        for row in rows:core.datetime.strptime(row['result']['captured_at'],'%Y-%m-%d %H:%M:%S.%f')
        self.assertEqual(readers.report()['requests'],4)
        self.assertEqual(readers.report()['sample_workers'],4)
        readers.close()
        self.assertTrue(all(client.closed for client in readers.clients.values()))
    def test_soft_404_does_not_cancel_other_samples_or_later_batches(self):
        barrier=threading.Barrier(4)
        def handler(client,spec):
            if spec['sample']<4:barrier.wait(timeout=3)
            if spec['sample']==1:raise core.ParseError('HTTP 404; no sample')
            return {'sample':spec['sample']}
        readers=self.readers(handler)
        rows=readers.request_many([{'sample':index} for index in range(4)])
        self.assertIsNone(rows[1]['result'])
        self.assertIsInstance(rows[1]['error'],core.ParseError)
        self.assertEqual([row['result']['data']['sample'] for row in rows if row['result']],[0,2,3])
        self.assertFalse(self.stop.is_set())
        self.assertEqual(readers.request_many([{'sample':4}])[0]['result']['data']['sample'],4)
    def test_nonobject_sample_is_soft_format_error(self):
        readers=self.readers(lambda client,spec:[])
        row=readers.request_many([{'sample':0}])[0]
        self.assertIsNone(row['result']);self.assertIsInstance(row['error'],core.ParseError)
        self.assertFalse(self.stop.is_set())
    def test_fatal_error_drains_running_samples_before_address_can_change(self):
        barrier=threading.Barrier(2);finished=threading.Event()
        def handler(client,spec):
            barrier.wait(timeout=3)
            if spec['sample']==0:raise core.ProtectionError('HTTP403')
            self.assertTrue(self.stop.wait(timeout=3))
            finished.set();return {'sample':1}
        readers=self.readers(handler,workers=2)
        with self.assertRaises(core.ProtectionError):
            readers.request_many([{'sample':0},{'sample':1},{'sample':2}])
        self.assertTrue(finished.is_set())
        self.assertTrue(self.stop.is_set())
        with self.assertRaises(core.BudgetError):readers.request_many([{'sample':3}])
    def test_nonbudget_fatal_is_preferred_after_all_futures_are_drained(self):
        barrier=threading.Barrier(2)
        def handler(client,spec):
            barrier.wait(timeout=3)
            if spec['sample']==0:raise core.BudgetError('Timer expired')
            raise OSError('Transport storage failed')
        readers=self.readers(handler,workers=2)
        with self.assertRaisesRegex(OSError,'storage failed'):
            readers.request_many([{'sample':0},{'sample':1}])
    def test_programming_errors_are_fatal(self):
        def handler(client,spec):raise ValueError('unexpected implementation fault')
        readers=self.readers(handler)
        with self.assertRaises(ValueError):readers.request_many([{'sample':0}])
        self.assertTrue(self.stop.is_set())
    def test_external_stop_interrupts_batch_and_refuses_future_work(self):
        started=threading.Event()
        def handler(client,spec):
            started.set();self.assertTrue(self.stop.wait(timeout=3))
            return {'sample':0}
        readers=self.readers(handler,workers=1)
        timer=threading.Thread(target=lambda:(started.wait(timeout=3),self.stop.set()))
        timer.start()
        with self.assertRaises(core.BudgetError):readers.request_many([{'sample':0},{'sample':1}])
        timer.join(timeout=3)
        with self.assertRaises(core.BudgetError):readers.request_many([{'sample':2}])
        self.assertEqual(readers.report()['requests'],1)
    def test_expired_deadline_starts_no_clients(self):
        with self.assertRaises(core.BudgetError):
            self.readers(lambda client,spec:{},deadline=time.monotonic()-1)
        self.assertTrue(self.stop.is_set())
    def test_constructor_wait_obeys_real_deadline_and_stop_signal(self):
        started=threading.Event()
        def factory(**options):
            started.set()
            if not options['stop_event'].wait(timeout=2):
                raise AssertionError('Constructor deadline did not signal stop')
            raise core.BudgetError('Initialization interrupted')
        beginning=time.monotonic()
        with self.assertRaises(core.BudgetError):
            SampleReaders(1,'http://127.0.0.1:9222',
                'https://www.ozon.ru/product/test-123/',3,beginning+.03,
                self.temp.name,self.stop,self.gate,client_factory=factory)
        self.assertTrue(started.is_set());self.assertTrue(self.stop.is_set())
        self.assertLess(time.monotonic()-beginning,1)
    def test_live_deadline_discards_late_response_and_stops_next_job(self):
        deadline=time.monotonic()+.03
        def handler(client,spec):
            while time.monotonic()<deadline:self.stop.wait(timeout=.005)
            return {'sample':spec['sample']}
        readers=self.readers(handler,workers=1,deadline=deadline)
        with self.assertRaises(core.BudgetError):
            readers.request_many([{'sample':0},{'sample':1}])
        self.assertEqual(readers.report()['requests'],1)
        self.assertTrue(self.stop.is_set())
    def test_all_readers_use_exact_existing_session_and_real_deadline(self):
        deadline=time.monotonic()+30
        readers=self.readers(lambda client,spec:{},deadline=deadline)
        paths=set()
        for index,client in readers.clients.items():
            options=client.options
            self.assertEqual(options['cdp'],'http://127.0.0.1:9222')
            self.assertEqual(options['page_url'],'https://www.ozon.ru/product/test-123/')
            self.assertEqual(options['product_mode'],'api')
            self.assertIs(options['isolated_context'],False)
            self.assertIs(options['stop_event'],self.stop)
            self.assertIs(options['backoff_gate'],self.gate)
            self.assertEqual(options['interval'],3)
            self.assertLessEqual(client.deadline,deadline)
            self.assertLessEqual(options['seconds'],30)
            paths.add(Path(options['state_path']).name)
        self.assertEqual(paths,{f'sample_reader_{i}.json' for i in range(1,5)})
    def test_close_drains_queued_jobs_and_closes_clients_on_owner_threads(self):
        started=threading.Event();release=threading.Event();outcomes=[]
        def handler(client,spec):
            if spec['sample']==0:
                started.set();self.assertTrue(release.wait(timeout=3))
            return {'sample':spec['sample']}
        readers=self.readers(handler,workers=1)
        caller=threading.Thread(target=lambda:outcomes.extend(readers.request_many([{'sample':0},{'sample':1}])))
        caller.start();self.assertTrue(started.wait(timeout=3))
        closer=threading.Thread(target=readers.close);closer.start()
        self.assertTrue(closer.is_alive());release.set()
        closer.join(timeout=5);caller.join(timeout=5)
        self.assertFalse(closer.is_alive());self.assertFalse(caller.is_alive())
        self.assertEqual([x['result']['data']['sample'] for x in outcomes],[0,1])
        self.assertTrue(all(client.closed for client in readers.clients.values()))
        with self.assertRaises(core.BudgetError):readers.request_many([{'sample':2}])
    def test_request_specs_are_copied_before_queueing(self):
        started=threading.Event();release=threading.Event();outcomes=[]
        def handler(client,spec):
            started.set();self.assertTrue(release.wait(timeout=3))
            return {'value':spec['nested']['value']}
        readers=self.readers(handler,workers=1)
        specs=[{'nested':{'value':'original'}}]
        caller=threading.Thread(target=lambda:outcomes.extend(readers.request_many(specs)))
        caller.start();self.assertTrue(started.wait(timeout=3))
        specs[0]['nested']['value']='changed';release.set();caller.join(timeout=5)
        self.assertFalse(caller.is_alive())
        self.assertEqual(outcomes[0]['result']['data']['value'],'original')


if __name__=='__main__':unittest.main()
