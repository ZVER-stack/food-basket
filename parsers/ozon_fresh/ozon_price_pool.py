"""Shared catalog/price readers in the current anonymous session.

The coordinator alone selects the house. All catalog and price futures must
finish before it changes address. No context/cookie replacement.
"""
from copy import deepcopy
from concurrent.futures import Future
from queue import Queue
import threading
import time
from pathlib import Path
import ozon_fresh_fast as core
from ozon_browser_transport import BrowserTransport
from ozon_product_prices import enrich_prices, MissingAmountError
from ozon_product_redirect import UnavailableProduct


class PricePool:
    def __init__(self,workers,cdp,page_url,interval,deadline,state_dir,stop_event,max_requests=6800,
                 client_factory=BrowserTransport,enricher=enrich_prices,backoff_gate=None,quarantine_incomplete=False,
                 allow_duplicate_test_tabs=False):
        self.jobs=Queue();self.threads=[];self.clients={};self.stop_event=stop_event
        self.request_counts={};self.count_lock=threading.Lock()
        self.enricher=enricher;self.closed=False;self.state_dir=Path(state_dir)
        self.quarantine_incomplete=quarantine_incomplete
        self.state_dir.mkdir(parents=True,exist_ok=True)
        self.options=dict(cdp=cdp,page_url=page_url,interval=interval,product_mode='api',
            stop_event=stop_event,max_requests=max_requests)
        if allow_duplicate_test_tabs:self.options['allow_duplicate_test_tabs']=True
        if backoff_gate:self.options['backoff_gate']=backoff_gate
        self.deadline=deadline;self.factory=client_factory
        ready=[]
        for index in range(1,workers+1):
            signal=Future();ready.append(signal)
            thread=threading.Thread(target=self._worker,args=(index,signal),name=f'fresh-price-{index}',daemon=True)
            self.threads.append(thread);thread.start()
        try:
            for signal in ready:signal.result()
        except Exception:
            self.stop_event.set();self.close();raise

    def _worker(self,index,ready):
        client=None
        try:
            client=self.factory(**self.options,seconds=max(.01,self.deadline-time.monotonic()),
                state_path=self.state_dir/f'price_worker_{index}.json')
            client.deadline=min(client.deadline,self.deadline)
            self.clients[index]=client
            with self.count_lock:self.request_counts[index]={'catalog_requests':0,'product_requests':0}
            ready.set_result(True)
            while True:
                job=self.jobs.get()
                if job is None:break
                kind,zone,item,future=job
                if not future.set_running_or_notify_cancel():continue
                before=client.count
                try:
                    if self.stop_event.is_set():raise core.BudgetError('Price pool stopped')
                    if kind=='catalog':
                        data=client.request(item)
                        result={'data':data,'captured_at':core.observed_now()}
                    else:result=self.enricher(client,zone,item)
                except (MissingAmountError,UnavailableProduct) as exc:
                    if kind=='product' and self.quarantine_incomplete:
                        partial=exc.item;partial['_quarantine_reason']=str(exc)
                        result=partial
                    else:
                        self.stop_event.set();self._count_requests(index,kind,client.count-before)
                        future.set_exception(exc);continue
                except Exception as exc:
                    self.stop_event.set();self._count_requests(index,kind,client.count-before)
                    future.set_exception(exc);continue
                self._count_requests(index,kind,client.count-before)
                future.set_result(result)
        except Exception as exc:
            self.stop_event.set()
            if not ready.done():ready.set_exception(exc)
        finally:
            if client:client.close()

    def _count_requests(self,index,kind,count):
        with self.count_lock:self.request_counts[index][kind+'_requests']+=count

    def request_async(self,request):
        """Queue a public catalog request on an existing reader thread.

        The caller owns the returned future and must drain it before switching
        the selected address. Receipt time belongs to the response, not the
        later moment when the coordinator decodes it.
        """
        if self.closed or self.stop_event.is_set():raise core.BudgetError('Price pool stopped')
        future=Future()
        self.jobs.put(('catalog',None,deepcopy(request),future))
        return future

    def enrich(self,zone,items):
        if self.closed or self.stop_event.is_set():raise core.BudgetError('Price pool stopped')
        futures=[]
        for item in items:
            future=Future();futures.append(future);self.jobs.put(('product',zone,item,future))
        # Wait for every submitted task, also on error. The coordinator may not
        # change the address while any old-house request is still in flight.
        rows=[];error=None
        for future in futures:
            try:rows.append(future.result())
            except Exception as exc:
                if error is None or (isinstance(error,core.BudgetError) and not isinstance(exc,core.BudgetError)):
                    error=exc
        if error:raise error
        return rows

    def close(self):
        if self.closed:return
        self.closed=True
        for _ in self.threads:self.jobs.put(None)
        deadline=time.monotonic()+35
        for thread in self.threads:thread.join(timeout=max(0,deadline-time.monotonic()))
        if any(thread.is_alive() for thread in self.threads):
            self.stop_event.set();raise core.ParseError('Price readers did not finish; keep Chrome stopped before changing its address')

    def report(self):
        with self.count_lock:counts=deepcopy(self.request_counts)
        return {'price_workers':len(self.threads),'requests':sum(client.count for client in self.clients.values()),
            'pool_role':'shared_catalog_and_product_readers',
            'catalog_requests':sum(x['catalog_requests'] for x in counts.values()),
            'product_requests':sum(x['product_requests'] for x in counts.values()),
            'rate_scope':'per_independent_reader','shared_anonymous_session':True,
            'independent_address_changes':False,'address_barrier':True,
            'workers':[{'worker':index,'requests':client.count,'blocked':bool(client.state.get('blocked')),
                **counts.get(index,{}),
                'reason':client.state.get('reason')} for index,client in sorted(self.clients.items())]}
