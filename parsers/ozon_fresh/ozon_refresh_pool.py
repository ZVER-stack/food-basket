"""Thread-owned readers for address-specific, selective public API samples.

Only the coordinator may select an address. request_many always drains its whole
batch before returning or raising, so no old-address request can leak into the
next address. An unavailable sample is distinct from a failed collection run.
"""
from concurrent.futures import Future, TimeoutError as FutureTimeout
from copy import deepcopy
from pathlib import Path
from queue import Queue
import threading
import time

import ozon_fresh_fast as core
from ozon_browser_transport import BrowserTransport


class SampleReaders:
    def __init__(self, workers, cdp, page_url, interval, deadline, state_dir,
                 stop_event, backoff_gate, client_factory=BrowserTransport):
        if not isinstance(workers, int) or workers < 1:
            raise ValueError('At least one sample reader is required')
        self.deadline=deadline;self.stop_event=stop_event
        self.state_dir=Path(state_dir);self.state_dir.mkdir(parents=True,exist_ok=True)
        self.jobs=Queue();self.threads=[];self.clients={}
        self.closed=False;self.submit_lock=threading.Lock();self.close_errors=[]
        self.options=dict(cdp=cdp,page_url=page_url,interval=interval,
            product_mode='api',isolated_context=False,stop_event=stop_event,
            backoff_gate=backoff_gate)
        self.factory=client_factory
        self._check()
        ready=[]
        try:
            for index in range(1,workers+1):
                signal=Future();ready.append(signal)
                thread=threading.Thread(target=self._worker,args=(index,signal),
                    name=f'fresh-sample-{index}',daemon=True)
                self.threads.append(thread);thread.start()
            for signal in ready:
                while True:
                    try:
                        signal.result(timeout=min(.1,max(.001,self.deadline-time.monotonic())))
                        break
                    except FutureTimeout:self._check()
        except Exception:
            self.stop_event.set()
            self.close()
            # A reader may have published the actual failure just as another
            # initializer noticed stop_event. Keep the original cause visible.
            for signal in ready:
                if signal.done():
                    error=signal.exception()
                    if error is not None and not isinstance(error,core.BudgetError):
                        raise error
            raise

    def _check(self):
        if self.stop_event.is_set() or time.monotonic()>=self.deadline:
            self.stop_event.set()
            raise core.BudgetError('Selective sample collection stopped or its deadline expired')

    def _worker(self,index,ready):
        client=None
        try:
            self._check()
            client=self.factory(**self.options,
                seconds=max(.001,self.deadline-time.monotonic()),
                state_path=self.state_dir/f'sample_reader_{index}.json')
            client.deadline=min(client.deadline,self.deadline)
            self.clients[index]=client
            ready.set_result(True)
            while True:
                job=self.jobs.get()
                if job is None:break
                spec,future=job
                if not future.set_running_or_notify_cancel():continue
                try:
                    self._check()
                    data=client.request(spec)
                    captured_at=core.observed_now()
                    self._check()
                    if not isinstance(data,dict):
                        raise core.ParseError('Sample API response is not a JSON object')
                except (core.ProtectionError,core.BudgetError,OSError) as exc:
                    self.stop_event.set();future.set_exception(exc)
                except core.ParseError as exc:
                    future.set_result({'result':None,'error':exc})
                except Exception as exc:
                    self.stop_event.set();future.set_exception(exc)
                else:
                    future.set_result({'result':{'data':data,'captured_at':captured_at},'error':None})
        except Exception as exc:
            self.stop_event.set()
            if not ready.done():ready.set_exception(exc)
        finally:
            if client:
                try:client.close()
                except Exception as exc:
                    self.stop_event.set();self.close_errors.append(exc)

    def request_many(self,specs):
        """Return ordered sample outcomes; drain every submitted job on failure."""
        self._check()
        try:specs=[deepcopy(spec) for spec in specs]
        except Exception:
            self.stop_event.set();raise
        futures=[]
        with self.submit_lock:
            if self.closed:raise core.BudgetError('Sample readers are closed')
            self._check()
            for spec in specs:
                future=Future();futures.append(future);self.jobs.put((spec,future))
        outcomes=[];fatal=None
        for future in futures:
            try:outcomes.append(future.result())
            except Exception as exc:
                outcomes.append(None)
                if fatal is None or (isinstance(fatal,core.BudgetError) and not isinstance(exc,core.BudgetError)):
                    fatal=exc
        if fatal is not None:raise fatal
        return outcomes

    def close(self):
        with self.submit_lock:
            if self.closed:return
            self.closed=True
            for _ in self.threads:self.jobs.put(None)
        deadline=time.monotonic()+35
        for thread in self.threads:thread.join(timeout=max(0,deadline-time.monotonic()))
        if any(thread.is_alive() for thread in self.threads):
            self.stop_event.set()
            raise core.ParseError('Sample readers did not finish; do not change the selected address')
        if self.close_errors:raise self.close_errors[0]

    def report(self):
        return {'sample_workers':len(self.threads),'requests':sum(client.count for client in self.clients.values()),
            'rate_scope':'per_independent_reader','shared_anonymous_session':True,
            'independent_address_changes':False,'address_barrier':True,
            'workers':[{'worker':index,'requests':client.count}
                for index,client in sorted(self.clients.items())]}
