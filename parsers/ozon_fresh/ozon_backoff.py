"""Shared cooldown for the existing anonymous session, explicitly user enabled.

No profile/identity changes. A CAPTCHA waits for an explicit manual signal.
"""
import os
import math
from pathlib import Path
import threading
import time
import ozon_fresh_fast as core


class BackoffGate:
    def __init__(self, output, stop_event, interval=3, clock=time.time, sleep=time.sleep,mode='full',
                 explicit_requested_interval=False,max_interval=None,on_403_interval=None,on_429_interval=None,
                 http_error_steps=None,http_error_statuses=(403,429)):
        self.output=Path(output);self.stop_event=stop_event
        self.clock=clock;self.sleep=sleep;self.lock=threading.RLock()
        self.mode=mode
        self.http_error_statuses=tuple(http_error_statuses)
        if not self.http_error_statuses or any(x not in (403,429) for x in self.http_error_statuses):
            raise ValueError("HTTP steps support only403/429")
        self.http_error_steps=tuple(http_error_steps or ())
        if http_error_steps is not None and (not self.http_error_steps or max_interval is None or
                (on_403_interval is not None and 403 in self.http_error_statuses) or
                (on_429_interval is not None and 429 in self.http_error_statuses) or
                any(not math.isfinite(x) or x<interval or x>max_interval for x in self.http_error_steps) or
                any(a>=b for a,b in zip(self.http_error_steps,self.http_error_steps[1:]))):
            raise ValueError('HTTP error steps must increase within rate bounds, without fixed HTTP intervals')
        self.http_error_level=0;self.http_error_last_burst=0
        if on_403_interval is not None and (not math.isfinite(on_403_interval) or
                on_403_interval<max(1,interval) or
                (max_interval is not None and on_403_interval>max_interval)):
            raise ValueError('The fixed403 interval must be finite and within the configured rate bounds')
        self.on_403_interval=on_403_interval
        self.fixed_403_active=False
        if on_429_interval is not None and (not math.isfinite(on_429_interval) or
                on_429_interval<max(1,interval) or
                (max_interval is not None and on_429_interval>max_interval)):
            raise ValueError('The fixed429 interval must be finite and within the configured rate bounds')
        self.on_429_interval=on_429_interval
        self.fixed_429_active=False
        self.until=0;self.failures=0;self.interval=max(3,interval)
        self.total_failures=0
        self.manual_since=0;self.last_notice=0
        self.last_failure_unix=0
        self.stable_since=0;self.successful_responses=0
        self.reason=None
        if max_interval is not None and (not math.isfinite(interval) or interval<1 or
                not math.isfinite(max_interval) or max_interval<interval):
            raise ValueError('Rate bounds must be finite and satisfy 1 <= interval <= max_interval')
        self.minimum_interval=interval if max_interval is not None else 3
        self.maximum_interval=max_interval if max_interval is not None else 60
        self.bounded_rate=max_interval is not None
        self.recovery_seconds=60 if self.bounded_rate else 600
        self.last_rate_save=self.clock()
        self.explicit_requested_interval=None
        self.signal=self.output/'manual_access_restored.json'
        state_path=self.output/'backoff_status.json'
        state={}
        if state_path.exists():
            state=core.load_json(state_path)
            self.until=state.get('retry_at_unix',0)
            self.failures=state.get('consecutive_failures',0)
            self.total_failures=state.get('total_protection_failures',self.failures)
            self.interval=max(self.interval,state.get('request_interval_seconds',3))
            self.manual_since=state.get('manual_required_since_unix',0)
            self.last_failure_unix=state.get('last_failure_unix',self.manual_since)
            self.reason=state.get('stop_reason')
            self.stable_since=state.get('stable_since_unix',0)
            self.successful_responses=state.get('successful_responses',0)
            self.fixed_403_active=bool(on_403_interval is not None and
                (state.get('fixed_403_interval_active') or self.is_http_403(self.reason)))
            self.fixed_429_active=bool(on_429_interval is not None and
                (state.get('fixed_429_interval_active') or self.is_http_429(self.reason)))
        if self.bounded_rate:
            configured=(state_path.exists() and
                state.get('minimum_request_interval_seconds')==self.minimum_interval and
                state.get('maximum_request_interval_seconds')==self.maximum_interval)
            # A newly chosen rate replaces an expired legacy rate once. Future
            # restarts keep this policy's throttled interval and recovery window.
            previous=(state.get('request_interval_seconds',interval) if state_path.exists() and
                (configured or self.manual_since or self.until>self.clock()) else interval)
            self.interval=min(self.maximum_interval,max(self.minimum_interval,
                previous))
        if self.fixed_403_active:self.interval=max(self.interval,self.on_403_interval)
        if self.fixed_429_active:self.interval=max(self.interval,self.on_429_interval)
        if self.http_error_steps:
            same_policy=(state.get('http_error_steps_seconds')==list(self.http_error_steps) and
                tuple(state.get('http_error_statuses',[403,429]))==self.http_error_statuses and
                state.get('minimum_request_interval_seconds')==self.minimum_interval and
                state.get('maximum_request_interval_seconds')==self.maximum_interval)
            if same_policy:
                self.http_error_level=min(len(self.http_error_steps),max(0,int(state.get('http_error_level',0))))
            # A newly chosen policy starts once at its base rate. Access waits,
            # manual checks and lifetime failure history are preserved.
            self.http_error_last_burst=max(self.last_failure_unix,state.get('http_error_last_burst_unix',0))
            self.interval=self.http_error_steps[self.http_error_level-1] if self.http_error_level else self.minimum_interval
            if same_policy:
                # A generic protection event can raise the shared pause without
                # advancing the403/429 staircase. Restart must retain that pause.
                self.interval=min(self.maximum_interval,max(self.interval,state.get('request_interval_seconds',interval)))
        if explicit_requested_interval:
            if not math.isfinite(interval) or interval<1:
                raise ValueError('Requested interval must be at least one second')
            if self.manual_since or self.until>self.clock():
                raise core.ProtectionError('Active cooldown/manual check must finish before applying the requested interval')
            # Explicit human configuration after an elapsed cooldown; retain
            # the failure history and escalate normally on any new403/429.
            self.interval=interval
            self.explicit_requested_interval=interval
            self.until=0;self.reason=None
            self._status('running')
        elif self.bounded_rate:
            self._status('waiting_manual_access' if self.manual_since else
                ('waiting_backoff' if self.until>self.clock() else 'running'))

    @staticmethod
    def is_http_403(reason):
        return 'http403' in str(reason or '').casefold().replace(' ','')

    @staticmethod
    def is_http_429(reason):
        return 'http429' in str(reason or '').casefold().replace(' ','')

    def _status(self, status, reason=None):
        state={'status':status,'pid':os.getpid(),'updated_at':core.now(),'mode':self.mode,
               'out_dir':str(self.output.resolve()),'complete':False,'stop_reason':reason or self.reason,
               'requires_manual_access_check':bool(self.manual_since),
               'retry_at_unix':self.until,'consecutive_failures':self.failures,
               'request_interval_seconds':self.interval,'manual_required_since_unix':self.manual_since}
        state['last_failure_unix']=self.last_failure_unix
        state['total_protection_failures']=self.total_failures
        if self.on_403_interval is not None:
            state.update(on_403_interval_seconds=self.on_403_interval,
                fixed_403_interval_active=self.fixed_403_active)
        if self.on_429_interval is not None:
            state.update(on_429_interval_seconds=self.on_429_interval,
                fixed_429_interval_active=self.fixed_429_active)
        if self.bounded_rate:
            state.update(minimum_request_interval_seconds=self.minimum_interval,
                maximum_request_interval_seconds=self.maximum_interval,
                rate_recovery_seconds=self.recovery_seconds)
        if self.explicit_requested_interval is not None:
            state['explicit_requested_interval_seconds']=self.explicit_requested_interval
        if self.http_error_steps:
            state.update(http_error_steps_seconds=list(self.http_error_steps),http_error_statuses=list(self.http_error_statuses),
                http_error_level=self.http_error_level,http_error_last_burst_unix=self.http_error_last_burst,
                automatic_decrease_after_http_error=False)
        state.update(stable_since_unix=self.stable_since,successful_responses=self.successful_responses)
        core.write_json(self.output/'backoff_status.json',state)
        core.write_json(self.output/'run_status.json',state)
        self.last_rate_save=self.clock()

    def penalize(self, reason, manual=False, retry_after=0,observed_at=None):
        with self.lock:
            now=self.clock()
            # A persisted failure happened before this process started. Its
            # original cooldown is preserved instead of starting it over now.
            happened=min(now,observed_at) if isinstance(observed_at,(int,float)) and 0<observed_at else now
            self.last_failure_unix=max(self.last_failure_unix,happened)
            self.stable_since=0;self.successful_responses=0
            self.reason=reason
            if self.http_error_steps and ((403 in self.http_error_statuses and self.is_http_403(reason)) or
                    (429 in self.http_error_statuses and self.is_http_429(reason))):
                # Concurrent readers share one escalation, including on replay.
                if happened>=self.until and happened>self.http_error_last_burst:
                    self.http_error_level=min(len(self.http_error_steps),self.http_error_level+1)
                    self.http_error_last_burst=happened
                    self.failures+=1;self.total_failures+=1
                    self.interval=max(self.interval,self.http_error_steps[self.http_error_level-1])
                    pause=self.interval
                else:
                    pause=max(0,self.until-happened)
                self.until=max(self.until,happened+pause,happened+retry_after)
                if manual:self.manual_since=self.manual_since or happened
                self._status('waiting_manual_access' if self.manual_since else 'waiting_backoff',reason)
                return
            fixed_403=self.on_403_interval is not None and self.is_http_403(reason)
            if fixed_403:
                self.fixed_403_active=True
                self.interval=max(self.interval,self.on_403_interval)
            fixed_429=self.on_429_interval is not None and self.is_http_429(reason)
            if fixed_429:
                self.fixed_429_active=True
                self.interval=max(self.interval,self.on_429_interval)
            # Concurrent failures in the same burst count as one escalation.
            if happened>=self.until:
                self.failures+=1
                self.total_failures+=1
                if not (fixed_403 or fixed_429):
                    self.interval=min(self.maximum_interval,max(self.minimum_interval,self.interval*2))
                pause=(self.on_403_interval if fixed_403 else self.on_429_interval if fixed_429
                    else min(1800,120*(2**min(self.failures-1,4))))
            else:
                pause=self.until-happened
            self.until=max(self.until,happened+pause,happened+retry_after)
            if manual:self.manual_since=self.manual_since or happened
            self._status('waiting_manual_access' if self.manual_since else 'waiting_backoff',reason)

    def require_manual(self,reason):
        """An already-open error page needs a check, not another network penalty."""
        with self.lock:
            self.reason=reason
            self.manual_since=self.manual_since or self.clock()
            self._status('waiting_manual_access',reason)

    def wait(self):
        while True:
            if self.stop_event.is_set():raise core.BudgetError('Collection interrupted')
            with self.lock:
                now=self.clock()
                if self.manual_since and self.signal.exists():
                    try:signal=core.load_json(self.signal)
                    except (ValueError,OSError):signal={}
                    if signal.get('confirmed_at_unix',0)>self.manual_since:
                        self.manual_since=0
                        self._status('waiting_backoff' if self.until>now else 'running')
                delay=self.until-now
                if delay<=0 and not self.manual_since:
                    if self.until:
                        self.until=0;self.reason=None;self._status('running')
                    return
                if now-self.last_notice>=30:
                    self.last_notice=now
                    self._status('waiting_manual_access' if self.manual_since else 'waiting_backoff')
            self.sleep(min(1,max(.05,delay)) if not self.manual_since else 1)

    def clear_after_wait(self, client):
        self.wait()
        client.state.update(blocked=False,min_interval=self.interval)
        client.state['previous_block_reason']=client.state.pop('reason',None)
        client.interval=(max(getattr(client,'base_interval',self.minimum_interval),self.interval)
            if self.http_error_steps else max(client.interval,self.interval))
        core.write_json(client.state_path,client.state)

    def note_success(self):
        """Recover gradually only after sustained successful public responses.

        This cannot shorten a cooldown or acknowledge a manual challenge.
        Bounded mode needs20 successes and60 healthy seconds per0.25s step.
        The legacy mode keeps20 successes and ten healthy minutes per halving.
        """
        with self.lock:
            now=self.clock()
            if self.manual_since or now<self.until:return
            if not self.stable_since:self.stable_since=now
            self.successful_responses+=1
            recovered=bool(self.bounded_rate and self.failures and
                self.successful_responses>=20 and now-self.stable_since>=self.recovery_seconds)
            if recovered:
                # Escalate consecutive failures, not all historical failures.
                # Keep lifetime count and last failure timestamp for audit.
                self.failures=0
            if self.http_error_steps:
                if recovered or now-self.last_rate_save>=30:self._status('running')
                return
            if (not (self.fixed_403_active or self.fixed_429_active) and self.interval>self.minimum_interval and self.successful_responses>=20 and
                    now-self.stable_since>=self.recovery_seconds):
                self.interval=max(self.minimum_interval,
                    round(self.interval-.25,10) if self.bounded_rate else self.interval/2)
                self.stable_since=now;self.successful_responses=0
                self._status('running')
            elif recovered or ((self.interval>self.minimum_interval or self.failures) and
                    now-self.last_rate_save>=30):
                self._status('running')
