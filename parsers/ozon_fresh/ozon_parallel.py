"""Independent anonymous address sessions, one request/s per worker as authorized."""
import argparse
from contextlib import closing
import csv
import hashlib
import json
import multiprocessing as mp
import sqlite3
import sys
import time
from pathlib import Path
import ozon_fresh_fast as core
import ozon_live_parser as live
from ozon_run_lock import profile_lock,browser_lock_path
from run_parser import preflight

ROOT=Path(__file__).resolve().parent

def partition_zones(zones,workers):
    parts=[zones[index::workers] for index in range(workers)]
    ids=[zone['h3_index'] for part in parts for zone in part]
    if len(ids)!=len(set(ids)) or set(ids)!={zone['h3_index'] for zone in zones}:
        raise core.ParseError('Zone partition lost or duplicated a house')
    return parts

def write_zone_file(path,zones):
    with Path(path).open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(zones[0]))
        writer.writeheader();writer.writerows(zones)

def worker_run(spec,stop_event):
    sys.stdout.reconfigure(encoding='utf-8');sys.stderr.reconfigure(encoding='utf-8')
    args=argparse.Namespace(**spec,stop_event=stop_event)
    try:
        code=live.run(args)
        if code:stop_event.set()
    except Exception as exc:
        stop_event.set()
        core.write_json(Path(args.out_dir)/'worker_error.json',{'reason':type(exc).__name__+': '+str(exc)[:300]})
        raise

def aggregate(root,expected_zones):
    rows=[];seen=set();done=set();reports=[]
    for folder in sorted(Path(root).glob('worker_*')):
        report=folder/'live_report.json'
        if report.exists():reports.append(core.load_json(report))
        state=folder/'snapshot.sqlite'
        if state.exists():
            with closing(sqlite3.connect(state)) as db:
                for zid,status,parsed_at,payload in db.execute("SELECT h3,status,parsed_at,payload FROM zones WHERE status IN ('complete','no_delivery')"):
                    if zid not in expected_zones or zid in done:
                        raise core.ParseError('Unexpected or duplicated completed zone')
                    products=json.loads(payload) if status=='complete' else [{}]
                    if not isinstance(products,list):raise core.ParseError('Invalid committed zone payload')
                    if status=='complete' and not products:raise core.ParseError('Completed zone has no target products')
                    for product in products:
                        row=dict(product,service='ozon_fresh',h3_index=zid,parsed_at=product.get('parsed_at') or parsed_at)
                        if set(row)-set(core.COLUMNS):raise core.ParseError('Committed row differs from the original template')
                        key=zid,row.get('product_id','')
                        if key in seen:raise core.ParseError('Duplicated product-zone pair')
                        if status=='complete' and any(row.get(field) in (None,'') for field in core.REQUIRED):
                            raise core.ParseError('Completed zone has a missing mandatory field')
                        seen.add(key);rows.append(row)
                    done.add(zid)
    temporary=Path(root)/'ozon_fresh.csv.tmp'
    with temporary.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=core.COLUMNS)
        writer.writeheader();writer.writerows(sorted(rows,key=lambda row:(row['h3_index'],str(row.get('product_id','')))))
    temporary.replace(Path(root)/'ozon_fresh.csv')
    return {'completed_zones':len(done & expected_zones),'rows':len(rows),'worker_reports':reports,
            'complete':done==expected_zones}

def aggregate_pilot(root,expected_zones):
    rows=[];seen=set();houses=set();reports=[]
    for folder in sorted(Path(root).glob('worker_*')):
        report=folder/'live_report.json'
        if report.exists():reports.append(core.load_json(report))
        path=folder/'pilot_observations.csv'
        if not path.exists():continue
        with path.open(encoding='utf-8-sig',newline='') as stream:
            reader=csv.DictReader(stream)
            if reader.fieldnames!=core.COLUMNS:raise core.ParseError('Pilot schema differs')
            for row in reader:
                key=row['h3_index'],row['product_id']
                if row['h3_index'] not in expected_zones or key in seen or any(not row[field] for field in core.REQUIRED):
                    raise core.ParseError('Pilot has an unexpected house, duplicate or missing mandatory field')
                seen.add(key);houses.add(row['h3_index']);rows.append(row)
    from ozon_har_catalog import write_csv
    write_csv(Path(root)/'pilot_observations.csv',rows)
    passed=houses==expected_zones and len(reports)>0 and all(not report['stop_reason'] for report in reports)
    return {'complete':False,'pilot_passed':passed,'sampled_zones':len(houses),'rows':len(rows),'worker_reports':reports,
        'note':'First pages of three categories only; full assortment is not measured.'}

def run(args):
    zones,excluded=core.load_zones(args.zones)
    if args.limit:zones=zones[:args.limit]
    workers=min(args.workers,len(zones))
    mode=getattr(args,'mode','full')
    if not workers:raise core.ParseError('No eligible zones')
    root=Path(args.out_dir);root.mkdir(parents=True,exist_ok=True)
    manifest_path=root/'parallel_manifest.json'
    fingerprint=hashlib.sha256(Path(args.zones).read_bytes()+Path(args.categories).read_bytes()+str((workers,args.limit,mode)).encode()).hexdigest()
    if (root/'protection_stop.json').exists():
        raise core.ProtectionError('Parallel run was stopped by protection. It will not create replacement sessions or retry automatically.')
    if manifest_path.exists():
        manifest=core.load_json(manifest_path)
        if manifest['fingerprint']!=fingerprint:raise core.ParseError('Parallel inputs changed; select another output directory')
    else:
        manifest={'fingerprint':fingerprint,'started_unix':time.time(),'workers':workers,'eligible_zones':len(zones),
            'excluded_no_house':len(excluded),'rate_scope':'per_independent_worker_authorized_by_user',
            'request_interval':args.interval,'isolated_contexts':True,'product_transport':'public_json_api'}
    if time.time()-manifest['started_unix']>=args.seconds:raise core.BudgetError('The common snapshot deadline expired')
    # Gate before creating any worker: reads the existing public product only.
    # A challenge in the user's connected profile never triggers new sessions.
    partitions=partition_zones(zones,workers)
    for index,part in enumerate(partitions,1):write_zone_file(root/f'zones_{index}.csv',part)
    context=mp.get_context('spawn');stop_event=context.Event();processes=[]
    interrupted=False
    with profile_lock(browser_lock_path(args.cdp)):
        try:
            ready=preflight(args.cdp)
        except Exception as exc:
            core.write_json(root/'connection_check.json',{'status':'stopped','reason':str(exc)[:300],
                'site_requests':0,'workers_started':0,'complete':False})
            raise
        core.write_json(root/'connection_check.json',ready)
        if not manifest_path.exists():
            manifest['started_unix']=time.time()
            core.write_json(manifest_path,manifest)
        try:
            for index in range(1,workers+1):
                if stop_event.is_set():break
                folder=root/f'worker_{index}';folder.mkdir(exist_ok=True)
                spec={'mode':mode,'cdp':args.cdp,'zones':str((root/f'zones_{index}.csv').resolve()),
                    'categories':str(Path(args.categories).resolve()),'out_dir':str(folder.resolve()),
                    'transport_state':str((folder/'transport_state.json').resolve()),'limit':0,
                    'interval':args.interval,'seconds':args.seconds,'max_requests':args.max_requests,
                    'retry_after_pause':False,'enrich_pilot_prices':True,'isolated_context':True,
                    'worker_id':index,'product_mode':'api',
                    'absolute_deadline':manifest['started_unix']+args.seconds}
                process=context.Process(target=worker_run,args=(spec,stop_event),name=f'fresh-worker-{index}')
                process.start();processes.append(process)
            last_status=0
            while any(process.is_alive() for process in processes):
                if time.time()-manifest['started_unix']>=args.seconds:stop_event.set()
                if any(process.exitcode not in (None,0) for process in processes):stop_event.set()
                if time.monotonic()-last_status>=30:
                    print('Workers active:',sum(process.is_alive() for process in processes),flush=True)
                    last_status=time.monotonic()
                for process in processes:process.join(timeout=0.1)
                time.sleep(0.2)
        except KeyboardInterrupt:
            interrupted=True;stop_event.set()
        finally:
            stop_event.set()
            for process in processes:process.join(timeout=35)
            for process in processes:
                if process.is_alive():process.terminate();process.join(timeout=5)
    protected=[]
    for path in root.glob('worker_*/transport_state.json'):
        state=core.load_json(path)
        if state.get('blocked'):protected.append({'worker':path.parent.name,'reason':state.get('reason'),'blocked_at_unix':state.get('blocked_at_unix')})
    if protected:core.write_json(root/'protection_stop.json',{'workers':protected,'no_automatic_retry':True})
    report=(aggregate if mode=='full' else aggregate_pilot)(root,{zone['h3_index'] for zone in zones})
    errors=[{'worker':process.name,'exitcode':process.exitcode} for process in processes if process.exitcode!=0]
    report.update(mode=mode,workers=workers,requested_zones=len(zones),excluded_no_house=len(excluded),
        elapsed_seconds=round(time.time()-manifest['started_unix'],2),interrupted=interrupted,
        protection_stops=protected,worker_errors=errors,two_hour_full_pass_confirmed=False)
    report['complete']=report['complete'] and not protected and not interrupted and not errors
    if mode=='pilot':report['pilot_passed']=report['pilot_passed'] and not protected and not interrupted and not errors
    report['two_hour_full_pass_confirmed']=bool(report['complete'] and len(zones)==197 and report['elapsed_seconds']<=7200)
    core.write_json(root/'parallel_report.json',report)
    print(json.dumps({key:value for key,value in report.items() if key!='worker_reports'},ensure_ascii=False,indent=2),flush=True)
    return 0 if report['complete'] or report.get('pilot_passed') else 2

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=('full','pilot'),default='full')
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--limit',type=int,default=0)
    parser.add_argument('--cdp',default='http://127.0.0.1:9222')
    parser.add_argument('--zones',default=str(ROOT/'hexagons.csv'))
    parser.add_argument('--categories',default=str(ROOT/'categories_live.json'))
    parser.add_argument('--out-dir',default=str(ROOT/'results/parallel_9222'))
    parser.add_argument('--interval',type=float,default=1.05)
    parser.add_argument('--seconds',type=int,default=7200)
    parser.add_argument('--max-requests',type=int,default=6800)
    args=parser.parse_args()
    if not 1<=args.workers<=8 or args.limit<0 or args.interval<1 or min(args.seconds,args.max_requests)<=0:
        parser.error('Use 1..8 workers, interval>=1 and positive budgets')
    try:return run(args)
    except Exception as exc:
        print('Parallel run stopped: '+str(exc)[:300],file=sys.stderr,flush=True)
        return 2

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
