"""Bounded, explicitly selective refresh of a completed full Fresh baseline.

The full collector remains a separate process/mode. Absence from samples never
removes a baseline product. Only checked/inferred-labelled valid rows enter CSV.
"""
import argparse,hashlib,json,math,os,threading,time
from pathlib import Path
from urllib.parse import quote,urlsplit,urlunsplit

import ozon_fresh_fast as core
import ozon_har_catalog as tiles
from ozon_browser_transport import BrowserTransport
from ozon_backoff import BackoffGate
from ozon_live_parser import FreshLocation
from ozon_product_prices import _enrich_prices_data,fresh_product_offer
from ozon_refresh_model import RefreshAccumulator
from ozon_refresh_state import Baseline,RefreshStore,choose_samples
from ozon_refresh_pool import SampleReaders
from ozon_run_lock import profile_lock,browser_lock_path
from ozon_runtime import system_awake

ROOT=Path(__file__).resolve().parent


def api_request(url):
    parts=urlsplit(core.public_url(url))
    if not parts.path.startswith(('/category/','/product/')):
        raise core.ParseError('Refresh accepts only public category/product links')
    return {'url':core.API+quote(urlunsplit(('','',parts.path,parts.query,'')),safe='')}


def verify_house(data,zone):
    address=tiles.address_title(data)
    if not address or tiles.normalized_address(address)!=tiles.normalized_address(zone['address']):
        raise core.ParseError('Refresh response does not confirm the selected house')


def inherit_access_state(output,baseline_path,results):
    """Carry cooldown/manual state forward, never reset it for a new pass."""
    if (output/'backoff_status.json').exists():return
    folders=[Path(baseline_path)]
    for name in ('active_shared_run.json','active_refresh_run.json'):
        pointer=results/name
        if pointer.exists():
            folder=Path(core.load_json(pointer)['out_dir'])
            if folder.resolve().is_relative_to(results.resolve()):folders.append(folder)
    choices=[]
    for folder in folders:
        path=folder/'backoff_status.json'
        if path.exists():choices.append((path.stat().st_mtime,folder,core.load_json(path)))
    if not choices:return
    _,folder,state=max(choices,key=lambda x:x[0])
    core.write_json(output/'backoff_status.json',state)
    signal=folder/'manual_access_restored.json'
    if signal.exists():core.write_json(output/'manual_access_restored.json',core.load_json(signal))


def signal_refresh_manual(root,cdp):
    """File06 may confirm an existing selective pass without starting a process."""
    results=Path(root)/'results';pointer=results/'active_refresh_run.json'
    if not pointer.exists():return False
    folder=Path(core.load_json(pointer)['out_dir']).resolve()
    if not folder.is_relative_to(results.resolve()):raise core.ParseError('Refresh output outside results')
    status=core.load_json(folder/'run_status.json')
    if not status.get('requires_manual_access_check'):return False
    # A newer full-collector episode takes priority over an older refresh.
    full=results/'active_shared_run.json'
    if full.exists():
        full_folder=Path(core.load_json(full)['out_dir'])
        latest=full_folder/'backoff_status.json'
        if latest.exists() and core.load_json(latest).get('manual_required_since_unix',0)>status.get('manual_required_since_unix',0):return False
    active=core.load_json(pointer)
    if active['started_unix']+active['seconds']<=time.time():
        print('Лимит повторного прохода истёк. Подтверждение не продлевает его и не запускает новый сбор.',flush=True)
        return True
    from run_parser import preflight
    preflight(cdp,allow_duplicate_test_tabs=True)
    core.write_json(folder/'manual_access_restored.json',{'confirmed_at_unix':time.time(),
        'source':'explicit_user_manual_check_launcher'})
    print('Ручная проверка повторного прохода подтверждена. Истёкший лимит времени не продлевается.',flush=True)
    return True


def run_refresh(args,baseline,store,zones,categories):
    output=Path(args.out);stop=threading.Event()
    final_deadline=store.started_unix+args.seconds
    reserve=min(60,max(1,args.seconds*.05))
    collection_end=final_deadline-reserve
    timer=threading.Timer(max(.001,collection_end-time.time()),stop.set);timer.daemon=True;timer.start()
    deadline=time.monotonic()+max(0,collection_end-time.time())
    gate=BackoffGate(output,stop,args.interval,mode='selective_refresh')
    client=readers=None;error=None;budget_ended=False;rows_report={}
    try:
        if time.time()>=collection_end:raise core.BudgetError('Selective pass collection budget exhausted')
        from run_parser import preflight
        while True:
            try:
                ready=preflight(args.cdp)  # document only, zero Ozon requests
                break
            except core.ProtectionError as exc:
                gate.penalize(str(exc),manual=True)
                gate.wait()
        gate.wait()
        client=BrowserTransport(cdp=args.cdp,interval=args.interval,seconds=max(.001,deadline-time.monotonic()),
            max_requests=math.inf,state_path=ROOT/'work/browser_transport_9222.json',
            page_url=ready['product_url'],product_mode='api',stop_event=stop,backoff_gate=gate)
        client.deadline=min(client.deadline,deadline)
        readers=SampleReaders(args.readers,args.cdp,ready['product_url'],args.interval,deadline,
            output/'sample_transports',stop,gate)
        location=FreshLocation(client)
        for number,zone in enumerate(zones,1):
            if store.done(zone['h3_index']):continue
            if stop.is_set() or time.time()>=collection_end:raise core.BudgetError('Selective pass collection budget exhausted')
            core.write_json(output/'run_status.json',{'mode':'selective_refresh','status':'running',
                'pid':os.getpid(),'updated_at':core.now(),'h3_index':zone['h3_index'],'zone_number':number,
                'requested_zones':len(zones),'complete':False,'catalog_fully_checked':False})
            old=baseline.house(zone['h3_index'])
            acc=RefreshAccumulator(zone,old,baseline.identity)
            previous=store.get_house(zone['h3_index'])
            if previous:
                acc.observations=previous['observations'];acc.pending=previous['pending']
                acc.seen=set(previous['summary'].get('seen_skus',[]))
                for pending in acc.pending:
                    acc._mark_unresolved(pending['product_id'],acc._stamp(pending['observed_at']))
            summary=acc.summary();summary['address_final_confirmed']=False
            store.save_house(zone['h3_index'],summary,acc.observations,acc.pending,'partial')
            context=location.switch(zone)
            if not context['delivery_available']:
                summary.update(delivery_available=False,address_final_confirmed=True)
                store.save_house(zone['h3_index'],summary,{},acc.pending,'no_delivery')
                continue
            # Under a tighter remaining budget keep scope small, while visiting
            # both food and chemicals. Budget failure leaves explicit unchecked IDs.
            remaining=len(zones)-number+1
            share=max(0,collection_end-time.time())/remaining
            page_limit=2 if share<6*gate.interval+20 else args.pages_per_house
            product_limit=0 if share<6*gate.interval+25 else args.max_product_checks
            plan=choose_samples(categories,baseline.category_pages(zone['h3_index']),
                zone['h3_index'],str(store.started_unix),pages=page_limit)
            candidates={};sample_errors=[]
            batch=readers.request_many([api_request(sample['url']) for sample in plan])
            for sample,outcome in zip(plan,batch):
                if outcome['error'] is not None:
                    sample_errors.append({'category_id':sample['category']['category_id'],
                        'page':sample['page'],'error':str(outcome['error'])});continue
                data=outcome['result']['data'];stamp=outcome['result']['captured_at']
                verify_house(data,zone)
                try:grid=tiles.primary_catalog_grid(data,require_layout=True)
                except core.ParseError as exc:
                    sample_errors.append({'category_id':sample['category']['category_id'],
                        'page':sample['page'],'error':str(exc)});continue
                for raw in grid['items']:
                    try:item=tiles.tile_product(raw)
                    except core.ParseError as exc:
                        sample_errors.append({'category_id':sample['category']['category_id'],
                            'product_id':str(raw.get('sku','')),'error':str(exc)});continue
                    result=acc.observe(sample['category'],item,stamp)
                    sku=item['product_id'];stamp=acc._stamp(stamp)
                    previous_candidate=candidates.get(sku)
                    if result['needs_product']:
                        if not previous_candidate or stamp>=previous_candidate[2]:
                            candidates[sku]=(sample['category'],result['item'],stamp)
                    elif previous_candidate and stamp>=previous_candidate[2]:candidates.pop(sku)
                summary=acc.summary();summary.update(address_final_confirmed=False,sample_errors=sample_errors)
                store.save_house(zone['h3_index'],summary,acc.observations,acc.pending,'partial')
            old_ids={row['product_id'] for row in old}
            candidates=sorted(candidates.values(),key=lambda x:(x[1]['product_id'] in old_ids,x[1]['product_id']))
            selected=candidates[:product_limit]
            for category,item,stamp in candidates[product_limit:]:
                acc.defer(category,item,stamp,'PDP not checked: selective time/request budget')
            products=readers.request_many([api_request(item['url']) for _,item,_ in selected])
            for (category,item,stamp),outcome in zip(selected,products):
                if outcome['error'] is not None:
                    acc.defer(category,item,stamp,str(outcome['error']));continue
                data=outcome['result']['data'];product_stamp=outcome['result']['captured_at']
                verify_house(data,zone)
                fresh_product_offer(data,item['product_id'])
                try:
                    enriched=_enrich_prices_data(data,zone,item,observed_at=product_stamp)
                    acc.accept(category,enriched,product_stamp)
                except core.ParseError as exc:acc.defer(category,item,stamp,str(exc))
            # request_many joined all old-house reads before this confirmation.
            location.confirm()
            summary=acc.summary();summary.update(address_final_confirmed=True,sample_errors=sample_errors,
                pages_requested=len(plan),PDP_checks_requested=len(selected),delivery_available=True)
            store.save_house(zone['h3_index'],summary,acc.observations,acc.pending,'sample_checked')
            core.write_json(output/'progress.json',{'mode':'selective_refresh','updated_at':core.now(),
                'zone_number':number,'requested_zones':len(zones),'h3_index':zone['h3_index'],
                'elapsed_seconds':round(time.time()-store.started_unix,2),'house':summary,
                'complete':False,'catalog_fully_checked':False},best_effort=True)
    except (Exception,KeyboardInterrupt) as exc:
        error=type(exc).__name__+': '+(str(exc) or 'Interrupted')
        budget_ended=isinstance(exc,core.BudgetError) and time.time()>=collection_end
    finally:
        stop.set();timer.cancel()
        if readers:
            try:readers.close()
            except Exception as exc:error=error or str(exc)
            rows_report=readers.report()
        if client:
            try:client.close()
            except Exception as exc:error=error or str(exc)
        report=store.export(zones,baseline)
        report.update(mode='selective_refresh',complete=False,full_catalog_complete=False,
            baseline_snapshot=baseline.identity,baseline_has_gaps=baseline.has_gaps,
            started_unix=store.started_unix,seconds_budget=args.seconds,
            elapsed_seconds=round(time.time()-store.started_unix,2),stop_reason=error,
            collection_budget_ended=budget_ended,reader_pool=rows_report,
            coordinator_requests=client.count if client else 0,
            catalog_replacements_due_to_absence_in_sample=0)
        report['finished_within_budget']=report['elapsed_seconds']<=args.seconds
        core.write_json(output/'refresh_report.json',report)
        state='sampled_finished' if report['sampling_plan_completed'] else ('deadline_finished' if budget_ended else 'stopped')
        core.write_json(output/'run_status.json',{'status':state,'mode':'selective_refresh','complete':False,
            'pid':os.getpid(),'updated_at':core.now(),'stop_reason':error,'out_dir':str(output.resolve()),
            'requires_manual_access_check':bool(gate.manual_since),
            'manual_required_since_unix':gate.manual_since,'sampling_plan_completed':report['sampling_plan_completed']})
    return 0 if not error or budget_ended else 2


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',help='Completed full snapshot directory; default active full snapshot')
    parser.add_argument('--zones',default=str(ROOT/'hexagons.csv'))
    parser.add_argument('--categories',default=str(ROOT/'categories_live.json'))
    parser.add_argument('--out');parser.add_argument('--plan',action='store_true')
    parser.add_argument('--seconds',type=int,default=7200)
    parser.add_argument('--interval',type=float,default=3)
    parser.add_argument('--readers',type=int,default=4)
    parser.add_argument('--pages-per-house',type=int,default=4)
    parser.add_argument('--max-product-checks',type=int,default=2)
    parser.add_argument('--cdp',default='http://127.0.0.1:9222')
    args=parser.parse_args(argv)
    if not(1<=args.seconds<=7200 and args.interval>=3 and 1<=args.readers<=4 and
           2<=args.pages_per_house<=8 and 0<=args.max_product_checks<=4):
        parser.error('Budget<=7200, interval>=3, readers1..4, pages2..8, PDP0..4 required')
    cdp=urlsplit(args.cdp)
    if cdp.hostname not in ('localhost','127.0.0.1') or cdp.port!=9222:parser.error('Use original local Chrome9222')
    baseline=store=None
    try:
        zones,_=core.load_zones(args.zones);categories=core.load_json(args.categories)
        if not categories or any(c.get('scope') not in ('food','household_chemicals') for c in categories):
            raise core.ParseError('Verified food/household category list required')
        fingerprint=hashlib.sha256(Path(args.zones).read_bytes()+Path(args.categories).read_bytes()).hexdigest()
        results=ROOT/'results'
        if not args.baseline:
            args.baseline=core.load_json(results/'active_shared_run.json')['out_dir']
        baseline=Baseline(args.baseline,zones,fingerprint)
        if args.plan:
            print(json.dumps({'mode':'selective_refresh','baseline':baseline.identity,'requested_zones':len(zones),
                'seconds_budget':args.seconds,'pages_per_house':args.pages_per_house,
                'max_product_checks_per_house':args.max_product_checks,'network_requests':0,
                'all_products_checked':False},ensure_ascii=False));return 0
        with profile_lock(browser_lock_path(args.cdp)),system_awake():
            pointer=results/'active_refresh_run.json'
            if not args.out and pointer.exists():
                previous=core.load_json(pointer)
                folder=Path(previous['out_dir']).resolve()
                if not folder.is_relative_to(results.resolve()):raise core.ParseError('Refresh output outside results')
                prior=core.load_json(folder/'run_status.json')
                if (previous['started_unix']+previous['seconds']>time.time() and
                    prior['status'] not in ('sampled_finished','deadline_finished')):args.out=str(folder)
            if not args.out:args.out=str(results/('refresh_'+core.now().replace('-','').replace(':','').replace(' ','_')))
            output=Path(args.out);output.mkdir(parents=True,exist_ok=True)
            inherit_access_state(output,Path(args.baseline).parent if Path(args.baseline).is_file() else args.baseline,results)
            store=RefreshStore(output,baseline.identity,fingerprint,args.seconds)
            core.write_json(pointer,{'out_dir':str(output.resolve()),'started_unix':store.started_unix,
                'seconds':args.seconds,'baseline':baseline.identity,'mode':'selective_refresh'})
            core.write_json(output/'run_status.json',{'status':'starting','mode':'selective_refresh',
                'pid':os.getpid(),'updated_at':core.now(),'complete':False})
            return run_refresh(args,baseline,store,zones,categories)
    except (core.ParseError,OSError,KeyError,ValueError) as exc:
        print('Повторный проход не запущен: '+str(exc),flush=True);return 2
    finally:
        if store:store.close()
        if baseline:baseline.close()


if __name__=='__main__':raise SystemExit(main())
