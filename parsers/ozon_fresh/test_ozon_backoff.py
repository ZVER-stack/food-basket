"""Offline backoff/quarantine scenarios; no site access."""
from pathlib import Path
import tempfile,threading,unittest
from unittest.mock import Mock,patch
import ozon_fresh_fast as core
from ozon_backoff import BackoffGate
from ozon_browser_transport import BrowserTransport
from ozon_price_pool import PricePool
from ozon_product_prices import MissingAmountError
from test_ozon_price_pool import FakeClient
from test_ozon_catalog_index import card,page,enriched,CAT
from ozon_live_parser import full_config
from ozon_catalog_index import CatalogIndex


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.event=threading.Event();self.now=1000
    def sleep(self,seconds):self.now+=seconds
    def gate(self):return BackoffGate(self.root,self.event,3,clock=lambda:self.now,sleep=self.sleep)
    def bounded(self):
        return BackoffGate(self.root,self.event,1.1,clock=lambda:self.now,sleep=self.sleep,max_interval=3)
    def fixed403(self):
        return BackoffGate(self.root,self.event,1.1,clock=lambda:self.now,sleep=self.sleep,
            max_interval=7,on_403_interval=7)
    def test_fixed403_waits_seven_seconds_and_never_grows_exponentially(self):
        gate=self.fixed403();self.assertEqual(gate.interval,1.1)
        for _ in range(5):
            gate.penalize('HTTP 403; increasing shared cooldown')
            self.assertEqual(gate.interval,7);self.assertEqual(gate.until,self.now+7)
            gate.wait()
    def test_fixed403_does_not_decrease_after_success_or_restart(self):
        gate=self.fixed403();gate.penalize('HTTP403');gate.wait()
        for _ in range(100):gate.note_success()
        self.now+=6000;gate.note_success()
        self.assertEqual(gate.interval,7)
        resumed=self.fixed403();self.assertEqual(resumed.interval,7)
        self.assertTrue(resumed.fixed_403_active)
        self.assertEqual(resumed.failures,0)
    def test_fixed403_preserves_server_retry_after_and_manual_check(self):
        gate=self.fixed403();gate.penalize('CAPTCHA in HTTP403',manual=True,retry_after=120)
        self.assertEqual(gate.until,self.now+120);self.assertTrue(gate.manual_since)
        self.now+=200
        for _ in range(100):gate.note_success()
        self.assertEqual(gate.interval,7);self.assertTrue(gate.manual_since)
        self.event.set()
        with self.assertRaises(core.BudgetError):gate.wait()
    def test_fixed403_keeps_existing_cooldown_and_429_rules(self):
        gate=self.fixed403();gate.penalize('HTTP429',retry_after=400)
        until=gate.until;self.assertEqual(until,self.now+400)
        gate.penalize('HTTP403')
        self.assertEqual(gate.until,until);self.assertEqual(gate.interval,7)
        gate.wait();gate.penalize('HTTP429')
        self.assertGreaterEqual(gate.until,self.now+120)
        self.assertEqual(gate.interval,7)
    def test_fixed403_can_restore_historical403_without_resetting_guards(self):
        core.write_json(self.root/'backoff_status.json',dict(request_interval_seconds=3,
            retry_at_unix=1200,consecutive_failures=8,last_failure_unix=800,
            manual_required_since_unix=800,stop_reason='HTTP 403; increasing shared cooldown'))
        gate=self.fixed403()
        self.assertEqual(gate.interval,7);self.assertEqual(gate.until,1200)
        self.assertEqual(gate.manual_since,800);self.assertEqual(gate.failures,8)
    def test_fixed403_setting_rejects_invalid_rate(self):
        for value in (0,float('nan'),float('inf'),8):
            with self.assertRaises(ValueError):
                BackoffGate(self.root,self.event,1.1,max_interval=7,on_403_interval=value)
    def test_fixed429_raises_interval_and_keeps_server_cooldown_without_recovery(self):
        gate=BackoffGate(self.root,self.event,1.1,clock=lambda:self.now,sleep=self.sleep,
            max_interval=7,on_403_interval=7,on_429_interval=7)
        gate.penalize('HTTP429',retry_after=500)
        self.assertEqual(gate.interval,7);self.assertEqual(gate.until,self.now+500)
        gate.wait()
        for _ in range(100):gate.note_success()
        self.now+=6000;gate.note_success();self.assertEqual(gate.interval,7)
        resumed=BackoffGate(self.root,self.event,1.1,clock=lambda:self.now,
            max_interval=7,on_403_interval=7,on_429_interval=7)
        self.assertTrue(resumed.fixed_429_active);self.assertEqual(resumed.interval,7)
    def test_fixed429_setting_rejects_invalid_rate(self):
        for value in (0,float('nan'),float('inf'),8):
            with self.assertRaises(ValueError):
                BackoffGate(self.root,self.event,1.1,max_interval=7,on_429_interval=value)
    def test_fixed_http10_applies_to_repeated_403_and_429_including_restart(self):
        def configured():
            return BackoffGate(self.root,self.event,10,clock=lambda:self.now,
                sleep=self.sleep,max_interval=10,on_403_interval=10,on_429_interval=10)
        gate=configured()
        for reason in ('HTTP429','HTTP403','HTTP 429','HTTP 403','HTTP429'):
            gate.penalize(reason)
            self.assertEqual((gate.interval,gate.until),(10,self.now+10))
            gate.wait()
            gate=configured()
        self.assertEqual(gate.total_failures,5)
    def test_fixed_http10_preserves_server_wait_and_requires_new_captcha_signal(self):
        gate=BackoffGate(self.root,self.event,10,clock=lambda:self.now,
            sleep=self.sleep,max_interval=10,on_403_interval=10,on_429_interval=10)
        core.write_json(gate.signal,{'confirmed_at_unix':999})
        gate.penalize('CAPTCHA in HTTP429',manual=True,retry_after=90)
        self.assertEqual(gate.until,1090)
        self.now=1100
        self.assertEqual(gate.manual_since,1000)
        self.event.set()
        with self.assertRaises(core.BudgetError):gate.wait()
    def test_user_can_change_latched_seven_to_five_without_clearing_access_wait(self):
        core.write_json(self.root/'backoff_status.json',dict(request_interval_seconds=7,
            minimum_request_interval_seconds=1.1,maximum_request_interval_seconds=7,
            fixed_403_interval_active=True,on_403_interval_seconds=7,
            retry_at_unix=1200,manual_required_since_unix=900,last_failure_unix=900))
        gate=BackoffGate(self.root,self.event,1.1,clock=lambda:self.now,sleep=self.sleep,
            max_interval=5,on_403_interval=5,on_429_interval=5)
        self.assertEqual(gate.interval,5);self.assertEqual(gate.until,1200)
        self.assertEqual(gate.manual_since,900)
    def test_five_second_policy_stays_five_for_both_error_types(self):
        gate=BackoffGate(self.root,self.event,1.1,clock=lambda:self.now,sleep=self.sleep,
            max_interval=5,on_403_interval=5,on_429_interval=5)
        gate.penalize('HTTP403');self.assertEqual(gate.until,self.now+5)
        gate.wait();gate.penalize('HTTP429',retry_after=500)
        self.assertEqual(gate.until,self.now+500);gate.wait()
        for _ in range(100):gate.note_success()
        self.now+=1000;gate.note_success();self.assertEqual(gate.interval,5)
    def test_bounded_rate_starts_at_requested_interval_and_caps_escalation(self):
        gate=self.bounded();self.assertEqual(gate.interval,1.1)
        gate.penalize('HTTP403');self.assertEqual(gate.interval,2.2)
        gate.wait();gate.penalize('HTTP429')
        self.assertEqual(gate.interval,3)
        gate.wait();gate.penalize('HTTP403');self.assertEqual(gate.interval,3)
    def test_new_bounded_policy_uses_requested_start_after_expired_legacy_pause(self):
        core.write_json(self.root/'backoff_status.json',dict(request_interval_seconds=12,
            retry_at_unix=900,consecutive_failures=8,last_failure_unix=500))
        gate=self.bounded()
        self.assertEqual(gate.interval,1.1);self.assertEqual(gate.failures,8)
        self.assertEqual(gate.last_failure_unix,500)
    def test_successful_period_resets_consecutive_failures_but_keeps_lifetime_history(self):
        core.write_json(self.root/'backoff_status.json',dict(request_interval_seconds=1.1,
            minimum_request_interval_seconds=1.1,maximum_request_interval_seconds=3,
            consecutive_failures=6,last_failure_unix=500))
        gate=self.bounded()
        for _ in range(20):gate.note_success()
        self.assertEqual(gate.failures,6)
        self.now+=60;gate.note_success()
        self.assertEqual(gate.failures,0);self.assertEqual(gate.total_failures,6)
        self.assertEqual(gate.last_failure_unix,500)
        resumed=self.bounded();self.assertEqual(resumed.failures,0)
        self.assertEqual(resumed.total_failures,6)
        resumed.penalize('New isolatedHTTP403')
        self.assertEqual(resumed.until,self.now+120)
        self.assertEqual(resumed.failures,1);self.assertEqual(resumed.total_failures,7)
    def test_success_history_cannot_reset_failure_count_during_pending_manual_check(self):
        gate=self.bounded();gate.penalize('CAPTCHA',manual=True)
        self.now+=1000
        for _ in range(100):gate.note_success()
        self.assertEqual(gate.failures,1);self.assertEqual(gate.total_failures,1)
        self.assertEqual(gate.successful_responses,0);self.assertTrue(gate.manual_since)
    def test_bounded_rate_retains_longer_retry_after_and_saved_manual_gate(self):
        core.write_json(self.root/'backoff_status.json',dict(request_interval_seconds=60,
            retry_at_unix=2000,manual_required_since_unix=500,consecutive_failures=8))
        gate=self.bounded();self.assertEqual(gate.interval,3)
        self.assertEqual(gate.until,2000);self.assertEqual(gate.failures,8)
        self.assertEqual(gate.manual_since,500)
        core.write_json(gate.signal,{'confirmed_at_unix':1001})
        gate.wait();self.assertEqual(self.now,2000)
        gate.penalize('HTTP429',retry_after=5000)
        self.assertEqual(gate.until,7000);self.assertEqual(gate.interval,3)
    def test_bounded_recovery_needs_healthy_time_and_successes_then_reaches_floor(self):
        gate=self.bounded();gate.penalize('HTTP403');gate.wait()
        for _ in range(30):gate.note_success()
        self.assertEqual(gate.interval,2.2)
        self.now+=60;gate.note_success();self.assertEqual(gate.interval,1.95)
        for expected in (1.7,1.45,1.2,1.1):
            for _ in range(20):gate.note_success()
            self.now+=60;gate.note_success();self.assertEqual(gate.interval,expected)
        self.now+=600
        for _ in range(100):gate.note_success()
        self.assertEqual(gate.interval,1.1)
    def test_bounded_captcha_and_cooldown_cannot_be_skipped_by_successes(self):
        gate=self.bounded();gate.penalize('CAPTCHA',manual=True)
        self.now+=600
        for _ in range(100):gate.note_success()
        self.assertEqual(gate.interval,2.2);self.assertEqual(gate.successful_responses,0)
        self.assertTrue(gate.manual_since)
    def test_bounded_rate_recovery_window_survives_restart(self):
        gate=self.bounded();gate.penalize('HTTP403');gate.wait();gate.note_success()
        for _ in range(20):gate.note_success()
        self.now+=30;gate.note_success()
        resumed=self.bounded();self.assertEqual(resumed.stable_since,gate.stable_since)
        self.now+=30;resumed.note_success();self.assertEqual(resumed.interval,1.95)
    def test_bounded_rate_rejects_invalid_bounds(self):
        for lower,upper in ((.9,3),(1.1,1),(float('nan'),3),(1.1,float('inf'))):
            with self.assertRaises(ValueError):
                BackoffGate(self.root,self.event,lower,max_interval=upper)
    def test_explicit_requested_rate_applies_after_elapsed_pause_and_retains_history(self):
        core.write_json(self.root/'backoff_status.json',{'request_interval_seconds':12,
            'retry_at_unix':900,'consecutive_failures':8,'last_failure_unix':500})
        gate=BackoffGate(self.root,self.event,1.1,clock=lambda:self.now,explicit_requested_interval=True)
        self.assertEqual(gate.interval,1.1);self.assertEqual(gate.failures,8)
        self.assertEqual(gate.last_failure_unix,500);self.assertEqual(gate.until,0)
        gate.penalize('New HTTP403')
        self.assertGreaterEqual(gate.interval,3);self.assertGreater(gate.until,self.now)
    def test_explicit_short_rate_cannot_skip_active_cooldown(self):
        core.write_json(self.root/'backoff_status.json',{'request_interval_seconds':12,'retry_at_unix':1100})
        with self.assertRaises(core.ProtectionError):
            BackoffGate(self.root,self.event,1.1,clock=lambda:self.now,explicit_requested_interval=True)
    def test_explicit_short_rate_cannot_clear_manual_challenge(self):
        core.write_json(self.root/'backoff_status.json',{'request_interval_seconds':12,
            'retry_at_unix':900,'manual_required_since_unix':500})
        with self.assertRaises(core.ProtectionError):
            BackoffGate(self.root,self.event,1.1,clock=lambda:self.now,explicit_requested_interval=True)
    def test_403_increases_pause_without_identity_change_and_honors_retry_after(self):
        gate=self.gate();gate.penalize('HTTP403')
        self.assertEqual((gate.until,gate.interval),(1120,6))
        gate.penalize('Another concurrent HTTP403')
        self.assertEqual(gate.failures,1)
        gate.wait();self.assertGreaterEqual(self.now,1120)
        gate.penalize('HTTP429',retry_after=400)
        self.assertEqual(gate.until,self.now+400)
        self.assertEqual(gate.interval,12)
    def test_persisted_failure_waits_only_the_remaining_original_cooldown(self):
        gate=self.gate();gate.penalize('EarlierHTTP403',observed_at=950)
        self.assertEqual(gate.until,1070)
        self.assertEqual(gate.last_failure_unix,950)
        gate.wait();self.assertEqual(self.now,1070)
    def test_expired_historical_pause_is_accounted_without_a_new_full_wait(self):
        gate=self.gate();gate.penalize('EarlierHTTP403',observed_at=500)
        gate.wait();self.assertEqual(self.now,1000)
        self.assertEqual(gate.failures,1)
        self.assertEqual(gate.interval,6)
    def test_old_manual_challenge_still_requires_a_fresh_signal(self):
        gate=self.gate();gate.penalize('EarlierCAPTCHA',manual=True,observed_at=500)
        self.assertEqual(gate.manual_since,500)
        self.event.set()
        with self.assertRaises(core.BudgetError):gate.wait()
    def test_observed_document_error_requires_check_without_inventing_a_new_http_failure(self):
        gate=self.bounded();gate.penalize('HTTP403')
        until=gate.until;failures=gate.failures;total=gate.total_failures
        gate.require_manual('Ozon shows no connection')
        self.assertEqual((gate.until,gate.failures,gate.total_failures),(until,failures,total))
        self.assertTrue(gate.manual_since)
        core.write_json(gate.signal,{'confirmed_at_unix':self.now+1})
        gate.wait();self.assertEqual(self.now,until)
    def test_captcha_requires_explicit_fresh_manual_signal(self):
        gate=self.gate();gate.penalize('CAPTCHA',manual=True)
        core.write_json(gate.signal,{'confirmed_at_unix':999})
        calls=[]
        def sleep(seconds):
            self.sleep(seconds);calls.append(1)
            if len(calls)==130:core.write_json(gate.signal,{'confirmed_at_unix':self.now})
        gate.sleep=sleep;gate.wait()
        self.assertGreaterEqual(len(calls),130)
        self.assertFalse(gate.manual_since)
    def test_transport_retries_only_after_shared_wait(self):
        client=object.__new__(BrowserTransport);client.backoff_gate=self.gate()
        client.state_path=self.root/'transport.json';client.state={'last_status':403,'blocked':True}
        client.interval=3
        client._request_once=Mock(side_effect=[core.ProtectionError('HTTP403'),{'ok':True}])
        self.assertEqual(client.request({'url':'unused'}),{'ok':True})
        self.assertEqual(client._request_once.call_count,2)
        self.assertGreaterEqual(self.now,1120)
        self.assertFalse(client.state['blocked'])
    def test_rate_recovers_only_after_twenty_successes_and_ten_healthy_minutes(self):
        gate=self.gate();gate.interval=60
        for _ in range(20):gate.note_success()
        self.assertEqual(gate.interval,60)
        self.now+=600;gate.note_success()
        self.assertEqual(gate.interval,30)
        for _ in range(19):gate.note_success()
        self.assertEqual(gate.interval,30)
        self.now+=600;gate.note_success()
        self.assertEqual(gate.interval,15)
    def test_success_cannot_skip_cooldown_or_clear_manual_access(self):
        gate=self.gate();gate.penalize('CAPTCHA',manual=True)
        for _ in range(100):gate.note_success()
        self.assertEqual(gate.interval,6)
        self.assertEqual(gate.successful_responses,0)
        self.now+=1000;gate.note_success()
        self.assertTrue(gate.manual_since)
        self.assertEqual(gate.interval,6)
    def test_new_protection_response_resets_success_window_and_persists_failure(self):
        gate=self.gate();gate.note_success();gate.penalize('HTTP403')
        self.assertEqual(gate.successful_responses,0)
        self.assertEqual(gate.stable_since,0)
        resumed=self.gate()
        self.assertEqual(resumed.last_failure_unix,self.now)
        self.assertEqual(resumed.until,gate.until)
    def test_transport_follows_recovered_shared_rate_instead_of_sticky_old_interval(self):
        client=object.__new__(BrowserTransport);client.backoff_gate=self.gate()
        client.base_interval=3;client.interval=60
        client._request_once=Mock(return_value={'ok':True})
        client.request({'url':'unused'})
        self.assertEqual(client.interval,3)
    def test_missing_amount_does_not_cancel_other_workers(self):
        def enrich(client,zone,item):
            if item['product_id']=='1':raise MissingAmountError('No amount',item)
            return dict(item,price='20')
        pool=PricePool(2,'http://127.0.0.1:9222','https://www.ozon.ru/product/test-123/',3,
            float('inf'),self.root,self.event,client_factory=FakeClient,enricher=enrich,quarantine_incomplete=True)
        self.addCleanup(pool.close)
        rows=pool.enrich({'address':'A'},[{'product_id':'1'},{'product_id':'2'}])
        self.assertEqual(rows[0]['_quarantine_reason'],'No amount')
        self.assertEqual(rows[1]['price'],'20')
        self.assertFalse(self.event.is_set())
    def test_root_parse_error_is_not_hidden_by_earlier_cancelled_future(self):
        barrier=threading.Barrier(2)
        def enrich(client,zone,item):
            barrier.wait(timeout=3)
            if item['product_id']=='1':raise core.BudgetError('Cancelled')
            raise core.ParseError('Actual parse failure')
        pool=PricePool(2,'http://127.0.0.1:9222','https://www.ozon.ru/product/test-123/',3,
            float('inf'),self.root,self.event,client_factory=FakeClient,enricher=enrich)
        self.addCleanup(pool.close)
        with self.assertRaisesRegex(core.ParseError,'Actual parse failure'):
            pool.enrich({'address':'A'},[{'product_id':'1'},{'product_id':'2'}])
    def test_incomplete_product_is_quarantined_and_catalog_continues_to_next_page(self):
        state=core.State(self.root/'state.sqlite','fixture');self.addCleanup(state.db.close)
        first=page([card('1')]);first['nextPage']=CAT['url']+'&page=2'
        second=page([card('2')])
        client=Mock();client.request.side_effect=[first,second]
        def enrich(item):
            if item['product_id']=='1':raise MissingAmountError('No amount',dict(item,amount=None))
            return enriched(item)
        cfg=full_config([CAT]);cfg['quarantine_incomplete']=True
        _,rows=core.collect_catalog(client,cfg,{},state,'A',enrich_product=enrich,catalog_index=CatalogIndex(state))
        self.assertEqual([r['product_id'] for r in rows],['2'])
        self.assertEqual(state.quarantined_for('A')[0]['product_id'],'1')
        self.assertEqual(client.request.call_count,2)
        state.mark('A','collected_with_gaps',rows)
        self.assertTrue(state.done('A'))
        self.assertEqual(state.export(self.root/'result.csv'),1)
    def test_ambiguous_catalog_response_is_saved_without_marking_page_complete(self):
        state=core.State(self.root/'state.sqlite','fixture');self.addCleanup(state.db.close)
        bad=page([card('1')]);bad['layout'][0]['widgetTrackingInfo']={'name':'shelf'}
        client=Mock();client.request.return_value=bad
        with self.assertRaisesRegex(core.ParseError,'Primary search results'):
            core.collect_catalog(client,full_config([CAT]),{},state,'A')
        saved=state.db.execute('SELECT product_id,error,payload FROM parse_failures').fetchall()
        self.assertEqual(len(saved),1);self.assertEqual(saved[0][0],'')
        self.assertEqual(__import__('json').loads(saved[0][2])['catalog_response'],bad)
        self.assertFalse(state.done('A'))
        self.assertEqual(state.db.execute('SELECT count(*) FROM pages').fetchone()[0],0)
    def test_expired_snapshot_continuation_requires_explicit_new_option(self):
        import run_parser
        folder=self.root/'existing'
        core.write_json(self.root/'active_shared_run.json',{'out_dir':str(folder.resolve()),
            'started_unix':0,'seconds':7200,'complete':False})
        with self.assertRaises(core.BudgetError):run_parser.shared_output(self.root,True)
        self.assertEqual(run_parser.shared_output(self.root,True,True),folder.resolve())

    def test_real_5in1_set_is_quarantined_without_invented_package_count(self):
        from ozon_product_prices import enrich_prices
        fixture=core.load_json(Path(__file__).parent/'testdata/quarantine_set_product.json')
        self.assertFalse(fixture['synthetic'])
        self.assertFalse(core.AMOUNT.findall(fixture['item']['name']))
        client=Mock();client.product_page.return_value=fixture['data']
        with self.assertRaises(MissingAmountError) as caught:
            enrich_prices(client,{'address':'Москва, Бакунинская улица, 60/2'},fixture['item'])
        self.assertIsNone(caught.exception.item['amount'])
        self.assertEqual(caught.exception.item['price'],'806')
        self.assertEqual(caught.exception.item['extra']['ordinary_price_source'],'product_webPrice')

    def test_windows_status_reader_lock_is_retried_without_losing_progress(self):
        original=Path.replace;attempts=[]
        def locked_then_available(path,target):
            attempts.append(1)
            if len(attempts)<3:raise PermissionError('Reader holds status file')
            return original(path,target)
        target=self.root/'progress.json'
        with patch.object(Path,'replace',locked_then_available),patch.object(core.time,'sleep'):
            self.assertTrue(core.write_json(target,{'page':7},best_effort=True))
        self.assertEqual(core.load_json(target),{'page':7})

    def test_long_progress_lock_does_not_disable_durable_guard_writes(self):
        with patch.object(Path,'replace',side_effect=PermissionError('Locked')),patch.object(core.time,'sleep'):
            self.assertFalse(core.write_json(self.root/'progress.json',{'page':7},best_effort=True))
            with self.assertRaises(PermissionError):core.write_json(self.root/'guard.json',{'blocked':True})

    def test_numeric_probe_checks_same_house_and_compares_saved_page_skus(self):
        from ozon_pagination_probe import run_queued_probe
        state=core.State(self.root/'state.sqlite','fixture');self.addCleanup(state.db.close)
        state.put_page('["A", "123"]',CAT['url']+'&page=2',core.now(),[{'product_id':'1'}],None)
        core.write_json(self.root/'pagination_probe_request.json',{'id':'fixture','h3_index':'A','category_id':'123','pages':[2]})
        data=page([card('1')]);data['widgetStates']['tileGridDesktop-fixture']['page']=2
        client=Mock();client.entrypoint.return_value=data
        location=Mock()
        run_queued_probe(self.root,{'h3_index':'A','address':'A'},[CAT],client,location,state)
        report=core.load_json(self.root/'pagination_probes/fixture/report.json')
        self.assertTrue(report['complete'])
        self.assertTrue(report['pages'][0]['same_skus_as_saved_sequential_page'])
        self.assertEqual(location.confirm.call_count,2)
        client.entrypoint.assert_called_once_with('/category/fixture-123/?miniapp=supermarket&page=2')
        run_queued_probe(self.root,{'h3_index':'A','address':'A'},[CAT],client,location,state)
        self.assertEqual(client.entrypoint.call_count,1)

    def test_numeric_probe_cannot_fetch_for_another_house_or_unbounded_page_list(self):
        from ozon_pagination_probe import run_queued_probe
        core.write_json(self.root/'pagination_probe_request.json',{'id':'fixture','h3_index':'B','category_id':'123','pages':[2]})
        client=Mock();location=Mock()
        run_queued_probe(self.root,{'h3_index':'A','address':'A'},[CAT],client,location,Mock())
        client.entrypoint.assert_not_called();location.confirm.assert_not_called()

if __name__=='__main__':unittest.main()
