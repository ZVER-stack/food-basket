"""Complete saved SKU metadata without recrawling catalogs or changing house prices."""
import argparse
import collections
import json
import math
import os
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
from playwright.sync_api import Error as PlaywrightError
import ozon_fresh_fast as core
import ozon_har_catalog as tiles
from ozon_house_export import MetadataStore,STATIC,REQUIRED_METADATA,identity,present,export,is_mass
from ozon_product_prices import fresh_product_offer,product_metadata,product_amount
from ozon_product_redirect import product_request,UnavailableProduct
from ozon_run_lock import exclusive_browser
from ozon_runtime import keep_system_awake

def read_metadata(client,zone,item):
    """Verify this SKU and house, then read only primary published details."""
    try:data=product_request(client,item)
    except UnavailableProduct as exc:return {'unavailable':str(exc)}
    except core.ParseError as exc:
        if str(exc)=='HTTP 404; no zone marked complete':
            return {'unavailable':'product_http404'}
        raise
    address=tiles.address_title(data)
    if not address or tiles.normalized_address(address)!=tiles.normalized_address(zone['address']):
        raise core.ParseError('Metadata page does not confirm the selected delivery house')
    normalize=lambda value:' '.join(str(value).split()).casefold().replace('ё','е')
    heading=(tiles.widget(data,'webProductHeading-') or {}).get('title','')
    if normalize(heading)!=normalize(item['name']):
        # Either a changed title or an unconfirmed/replaced primary SKU is
        # a per-product review case. Neither is eligible for metadata copying.
        # Transport access guards and the exact-house check above remain fatal.
        confirmed=False
        try:fresh_product_offer(data,item['product_id']);confirmed=True
        except core.ParseError as exc:
            if str(exc)!='Product page has no matching Fresh offer for the requested SKU':raise
        try:observed_amount,_=product_amount(data)
        except core.ParseError:observed_amount=None
        return {'identity_conflict':{'expected_sku':str(item['product_id']),
            'saved_name':item['name'],'actual_name':heading,'saved_amount':item.get('amount'),
            'actual_amount':observed_amount,'exact_fresh_sku_confirmed':confirmed,
            'primary_cart_skus':[str(v.get('firstButton',{}).get('sku')) for k,v in tiles.widgets(data).items()
                if k.startswith('webAddToCart-') and isinstance(v,dict) and v.get('firstButton',{}).get('sku') is not None],
            'source_h3':zone['h3_index'],'source_url':item['url'],'observed_at':core.observed_now()}}
    try:fresh_product_offer(data,item['product_id'])
    except core.ParseError as exc:
        if str(exc)=='Product page has no matching Fresh offer for the requested SKU':
            return {'unavailable':'no_current_fresh_offer'}
        raise
    metadata,characteristics=product_metadata(data)
    try:amount,_=product_amount(data)
    except core.ParseError:amount=None
    link=(tiles.widget(data,'paginator-') or {}).get('nextPage')
    if link:
        target=urlsplit(core.public_url(link));original=urlsplit(core.public_url(item['url']))
        query=parse_qs(target.query)
        if (target.path!=original.path or query.get('layout_page_index')!=['2'] or
                not query.get('layout_container',[''])[0].startswith('pdp')):
            raise core.ParseError('Metadata continuation differs from the verified SKU')
        try:details=product_request(client,item,path=target.path+'?'+target.query)
        except UnavailableProduct as exc:return {'unavailable':str(exc)}
        header=tiles.address_title(details)
        if header and tiles.normalized_address(header)!=tiles.normalized_address(zone['address']):
            raise core.ParseError('Metadata continuation changed the verified house')
        additional,chars=product_metadata(details);metadata.update(additional);characteristics.update(chars)
        try:amount,_=product_amount(details)
        except core.ParseError:pass
    if is_mass(amount):metadata['weight']=amount
    return {'metadata':{k:v for k,v in metadata.items() if k in STATIC or k=='weight'},
            'characteristics':characteristics,'observed_at':core.observed_now(),
            'missing_on_site':[k for k in (*REQUIRED_METADATA,'weight') if not present(metadata.get(k))],
            'source_h3':zone['h3_index'],'source_url':item['url'],'schema':2}

def apply_result(entry,h3,result):
    """An unavailable house is tried once; protection failures never call this."""
    entry=json.loads(json.dumps(entry))
    entry['attempts'].append({'h3':h3,'at':core.observed_now(),
                              'result':result.get('unavailable') or ('identity_conflict' if result.get('identity_conflict') else 'checked_primary_product')})
    if result.get('identity_conflict'):
        entry['status']='identity_conflict'
        entry['attempts'][-1]['identity_conflict']=result['identity_conflict']
        return entry
    if result.get('unavailable'):
        attempted={a['h3'] for a in entry['attempts']}
        if all(s['h3'] in attempted for s in entry['sources']):entry['status']='unavailable_at_known_houses'
        return entry
    for field,value in result['metadata'].items():
        if present(value):entry['metadata'][field]={'value':value,'at':result['observed_at'],
            'h3':h3,'source':'verified_primary_product','url':result['source_url']}
    entry['checked_at']=result['observed_at']
    entry['status']='checked_missing_on_site' if result['missing_on_site'] else 'checked_complete'
    return entry

def pick_sources(entries,current_h3):
    groups=collections.defaultdict(list)
    for key,entry in entries.items():
        if entry['status']!='pending':continue
        attempted={a['h3'] for a in entry['attempts']}
        sources=[s for s in reversed(entry['sources']) if s['h3'] not in attempted]
        if sources:
            source=next((s for s in sources if s['h3']==current_h3),sources[0])
            groups[source['h3']].append((key,source['row']))
    return sorted(groups.items(),key=lambda pair:(pair[0]!=current_h3,-len(pair[1])))

def check_guards(snapshot):
    for name in ('run_status.json','backoff_status.json'):
        path=Path(snapshot)/name
        state=core.load_json(path) if path.exists() else {}
        if state.get('requires_manual_access_check') or state.get('manual_required_since_unix'):
            raise core.ProtectionError('A new direct human confirmation is required before metadata requests')

@exclusive_browser
@keep_system_awake
def run(args):
    from ozon_backoff import BackoffGate
    from ozon_browser_transport import BrowserTransport
    from ozon_live_parser import FreshLocation
    from ozon_price_pool import PricePool
    from run_parser import preflight
    check_guards(args.snapshot)
    authorization=core.load_json(args.authorization)
    if not authorization.get('active') or authorization.get('task_scope')!='metadata_backfill_only':
        raise core.ParseError('Metadata-only authorization is inactive')
    manual_document=Path(args.authorization).parent/'latest_metadata_manual_check.json'
    preferred_sku=core.load_json(manual_document).get('sku') if manual_document.exists() else None
    ready=preflight(args.cdp,allow_duplicate_test_tabs=True,preferred_sku=preferred_sku)
    output=Path(args.snapshot);job_dir=Path(args.job_dir);job_dir.mkdir(parents=True,exist_ok=True)
    stop=threading.Event();lock=threading.RLock();client=pool=None
    store=MetadataStore(args.store);entries=store.entries();store.close()
    counters={'products_checked_this_launch':0,'unavailable_this_launch':0,'identity_conflicts_this_launch':0}
    current_h3=None;error=None;completed=False;last_export=time.monotonic();last_export_count=0
    def status(state,**kwargs):
        core.write_json(job_dir/'status.json',{'status':state,'pid':os.getpid(),'updated_at':core.now(),
            'source_snapshot':str(output),'output_csv':args.output,'catalog_recrawl':False,
            'source_prices_unchanged':True,'current_h3':current_h3,**counters,
            'metadata_statuses':dict(collections.Counter(e['status'] for e in entries.values())),**kwargs})
    def authorization_watch():
        while not stop.wait(2):
            try:active=core.load_json(args.authorization).get('active')
            except (OSError,ValueError):active=False
            if not active:stop.set();return
    threading.Thread(target=authorization_watch,daemon=True).start()
    gate=BackoffGate(output,stop,5,max_interval=10,http_error_steps=(6,7,8,9,10),mode='metadata_backfill')
    def checkpoint_reader(worker,zone,item):
        check_path=job_dir/'product_checks'/worker.state_path.name
        check={'status':'requesting','item':item,'h3':zone['h3_index'],'started_at':core.observed_now()}
        core.write_json(check_path,check)
        try:result=read_metadata(worker,zone,item)
        except Exception as exc:
            core.write_json(check_path,dict(check,status='failed',error=str(exc),error_type=type(exc).__name__,finished_at=core.observed_now()))
            raise
        with lock:
            key=identity(item);entry=apply_result(entries[key],zone['h3_index'],result)
            db=MetadataStore(args.store)
            try:db.save(key,entry);db.db.commit()
            finally:db.close()
            entries[key]=entry
            core.write_json(check_path,dict(check,status=entry['status'],identity_conflict=result.get('identity_conflict'),finished_at=core.observed_now()))
            counters['products_checked_this_launch']+=not bool(result.get('unavailable') or result.get('identity_conflict'))
            counters['unavailable_this_launch']+=bool(result.get('unavailable'))
            counters['identity_conflicts_this_launch']+=bool(result.get('identity_conflict'))
            status('running',last_sku=item['product_id'],last_result=result.get('unavailable') or entry['status'])
            print(json.dumps({'at':core.now(),'sku':item['product_id'],'h3':zone['h3_index'],
                'status':entry['status'],'missing_on_site':result.get('missing_on_site'),
                'identity_conflict':result.get('identity_conflict'),**counters},ensure_ascii=True),flush=True)
        return result
    try:
        gate.wait()
        client=BrowserTransport(cdp=args.cdp,interval=5,seconds=math.inf,max_requests=math.inf,
            state_path=job_dir/'coordinator.json',stop_event=stop,product_mode='api',
            page_url=ready['product_url'],backoff_gate=gate,allow_duplicate_test_tabs=True)
        zones,_=core.load_zones(args.zones);zone_map={z['h3_index']:z for z in zones}
        categories=core.load_json(args.categories)
        location=FreshLocation(client,confirmation_category=categories[0])
        progress=core.load_json(output/'progress.json')
        if progress.get('h3_index') in zone_map and tiles.normalized_address(ready.get('address') or '')==tiles.normalized_address(zone_map[progress['h3_index']]['address']):
            current_h3=progress['h3_index']
            location.current_zone=zone_map[current_h3];location.delivery_available=True
            location.confirm()
        pool=PricePool(7,args.cdp,ready['product_url'],5,math.inf,job_dir/'price_transports',stop,
            max_requests=math.inf,enricher=checkpoint_reader,backoff_gate=gate,allow_duplicate_test_tabs=True)
        status('running',workers=7)
        while True:
            groups=pick_sources(entries,current_h3)
            if not groups:completed=True;break
            for h3,items in groups:
                if stop.is_set():raise core.BudgetError('Metadata completion interrupted')
                zone=zone_map[h3]
                if h3!=current_h3:
                    context=location.switch(zone)
                    if not context.get('delivery_available'):
                        with lock:
                            db=MetadataStore(args.store)
                            try:
                                for key,item in items:
                                    entries[key]=apply_result(entries[key],h3,{'unavailable':'no_current_delivery'})
                                    db.save(key,entries[key])
                                db.db.commit()
                            finally:db.close()
                        continue
                    current_h3=h3
                for offset in range(0,len(items),7):
                    pool.enrich(zone,[item for key,item in items[offset:offset+7]])
                    if (counters['products_checked_this_launch']-last_export_count>=100 or time.monotonic()-last_export>=600):
                        status('refreshing_csv')
                        export(args.snapshot,args.zones,args.store,args.output)
                        last_export=time.monotonic();last_export_count=counters['products_checked_this_launch']
                    status('running',workers=7,transport=pool.report())
    except Exception as exc:
        error=str(exc)
        print(json.dumps({'error':error,'type':type(exc).__name__},ensure_ascii=True),flush=True)
        raise
    finally:
        stop.set()
        try:
            if pool:pool.close()
        finally:
            if client:client.close()
            report=export(args.snapshot,args.zones,args.store,args.output)
            manual=bool(gate.manual_since)
            status('complete' if completed else 'waiting_manual_access' if manual else 'stopped',error=error,coverage=report)
            if not manual:
                core.write_json(output/'run_status.json',{'status':'metadata_complete' if completed else 'stopped',
                    'pid':os.getpid(),'updated_at':core.now(),'mode':'metadata_backfill','complete':False,
                    'requires_manual_access_check':False,'stop_reason':error,'out_dir':str(output)})

def run_with_network_recovery(args,execute=run,max_retries=3,drain_seconds=35,
                              clock=time.time,sleep=time.sleep):
    """Only the known transient timeout resumes; guards and checkpoints survive."""
    job_dir=Path(args.job_dir);job_dir.mkdir(parents=True,exist_ok=True)
    recovery_path=job_dir/'network_recovery.json'
    recovery=core.load_json(recovery_path) if recovery_path.exists() else {'attempts':0,'history':[]}
    def allowed():
        authorization=core.load_json(args.authorization)
        if not authorization.get('active') or authorization.get('task_scope')!='metadata_backfill_only':
            raise core.BudgetError('Metadata authorization is inactive')
        check_guards(args.snapshot)
    while True:
        allowed()
        try:return execute(args)
        except (core.ParseError,PlaywrightError) as exc:
            timeout=str(exc)=='Browser request failed or timed out; no automatic retry'
            disconnected=(isinstance(exc,PlaywrightError) and
                'Target page, context or browser has been closed' in str(exc))
            if isinstance(exc,core.ProtectionError) or not (timeout or disconnected):
                raise
            allowed()
            if recovery['attempts']>=max_retries:
                recovery.update(exhausted=True,last_error=str(exc),updated_unix=clock())
                core.write_json(recovery_path,recovery)
                raise
            recovery['attempts']+=1
            retry_at=clock()+drain_seconds
            recovery['history'].append({'attempt':recovery['attempts'],'error':str(exc),
                'at_unix':clock(),'retry_at_unix':retry_at})
            core.write_json(recovery_path,recovery)
            status_path=job_dir/'status.json'
            state=core.load_json(status_path) if status_path.exists() else {}
            state.update(status='waiting_network_retry',pid=os.getpid(),updated_at=core.now(),
                network_retry_attempt=recovery['attempts'],network_retry_at_unix=retry_at,error=str(exc))
            core.write_json(status_path,state)
            print(json.dumps({'event':'network_retry_scheduled','attempt':recovery['attempts'],
                'retry_at_unix':retry_at,'checkpoints_preserved':True}),flush=True)
            while clock()<retry_at:
                allowed();sleep(min(1,retry_at-clock()))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('snapshot','zones','categories','store','output','authorization','job-dir'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--cdp',default='http://127.0.0.1:9222')
    args=parser.parse_args()
    run_with_network_recovery(args)

if __name__=='__main__':main()
