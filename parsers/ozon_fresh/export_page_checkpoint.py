"""Export committed page observations explicitly as an incomplete checkpoint."""
import argparse,csv,json,sqlite3
from contextlib import closing
from pathlib import Path
import ozon_fresh_fast as core

def export_checkpoint(state,zones_path,out):
 zones,_=core.load_zones(zones_path);addresses={z['h3_index']:z['address'] for z in zones}
 observations={};categories={}
 with closing(sqlite3.connect(Path(state).resolve().as_uri()+'?mode=ro',uri=True)) as db:
  started=db.execute("SELECT value FROM meta WHERE key='started_at'").fetchone()[0]
  done={r[0] for r in db.execute("SELECT h3 FROM zones WHERE status IN ('complete','collected_with_gaps','no_delivery')")}
  pages=list(db.execute('SELECT cache_key,captured_at,payload FROM pages ORDER BY captured_at,url'))
 for cache_key,stamp,payload in pages:
  zid,category=json.loads(cache_key)
  if zid in done:continue
  if zid not in addresses:raise core.ParseError('Checkpoint has an unknown house')
  for product in json.loads(payload)['rows']:
   row=dict(product,service='ozon_fresh',h3_index=zid,parsed_at=product.get('parsed_at') or stamp)
   if any(row.get(field) in (None,'') for field in core.REQUIRED):raise core.ParseError('Checkpoint has a missing mandatory field')
   key=zid,row['product_id'];categories.setdefault(key,set()).add(str(category))
   previous=observations.get(key)
   if previous:
    from ozon_catalog_index import merge_observation
    row=merge_observation(previous,row)
   extra=json.loads(row.get('extra') or '{}')
   extra.update(capture_type='incomplete_page_checkpoint',full_zone_complete=False,
    source_snapshot_started_at=started,delivery_address=addresses[zid],source_category_ids=sorted(categories[key]))
   row['extra']=json.dumps(extra,ensure_ascii=False);observations[key]=row
 out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
 temporary=out.with_suffix('.csv.tmp')
 with temporary.open('w',encoding='utf-8',newline='') as stream:
  writer=csv.DictWriter(stream,fieldnames=core.COLUMNS);writer.writeheader()
  writer.writerows(observations[key] for key in sorted(observations))
 temporary.replace(out)
 report={'complete':False,'kind':'incomplete_page_checkpoint','rows':len(observations),
  'houses_with_partial_observations':len({key[0] for key in observations}),'snapshot_started_at':started,
  'completed_zones_excluded':len(done),'note':'Not the full assortment of a zone and not the completed197-zone snapshot.'}
 core.write_json(out.with_suffix('.report.json'),report)
 return report
if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--state',required=True);parser.add_argument('--zones',required=True);parser.add_argument('--out',required=True)
 args=parser.parse_args();print(json.dumps(export_checkpoint(args.state,args.zones,args.out),ensure_ascii=True))
