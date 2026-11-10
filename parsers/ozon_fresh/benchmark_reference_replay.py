"""Replay saved catalog observations; counts are not a live latency/accuracy test."""
from copy import deepcopy
from pathlib import Path
import json,sqlite3,time
from contextlib import closing
import ozon_fresh_fast as core
from ozon_catalog_index import CatalogIndex,extra_of,needs_product

root=Path(r'C:\Users\maxik\PycharmProjects\PythonProject\fresh_fast')
path=root/'results/full_shared_20261018_111443/snapshot.sqlite'
with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as source:
    fingerprint=source.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()[0]
    house=source.execute("SELECT h3 FROM zones WHERE status IN ('complete','collected_with_gaps') ORDER BY parsed_at,h3 LIMIT 1").fetchone()[0]
    prefix=json.dumps([house])[:-1]+','
    observations=[];page_count=0
    for payload, in source.execute('SELECT payload FROM pages WHERE cache_key>=? AND cache_key<? ORDER BY captured_at,url',(prefix,prefix+'\uffff')):
        page_count+=1
        observations.extend(json.loads(payload)['rows'])

def replay(reference):
    state=core.State(':memory:',fingerprint);started=time.perf_counter()
    try:
        index=CatalogIndex(state,True,path if reference else None)
        local={};calls=0;unusable=0
        for row in observations:
            extra=extra_of(row);quote=extra.get('catalog_quote')
            if not quote:unusable+=1;continue
            item={k:deepcopy(v) for k,v in quote.items() if k not in ('name','price_style')}
            item.update(product_id=row['product_id'],name=row['name'],url=row.get('url'),
                extra={'price_style':quote.get('price_style'),'fresh_action_verified':extra.get('fresh_action_verified')})
            resolved=index.resolve(house,item,local)
            if needs_product(resolved):
                calls+=1;local[row['product_id']]=row
            else:local[row['product_id']]=dict(row,**resolved)
        return {'planned_product_requests':calls,'historical_inferred_cache_hits':index.stats['historical_cache_hits'],
            'imported_reference_houses':index.stats['imported_reference_houses'],
            'replayed_products':len(local),'unusable_saved_signatures':unusable,
            'local_replay_seconds':round(time.perf_counter()-started,3)}
    finally:state.db.close()

before=replay(False);after=replay(True)
report={'checked_at_msk':core.now(),'kind':'offline_saved_response_request_plan_comparison',
    'live_runtime_measured':False,'ordinary_price_accuracy_now_verified':False,
    'assumption':'Saved visible quotes replay unchanged; imported ordinary prices are explicitly historical/inferred',
    'house':house,'saved_catalog_pages':page_count,'saved_observations':len(observations),
    'comparison_scope':'Only saved observations with original catalog_quote; older observations without that signature are skipped in both cases',
    'without_historical_reference':before,'with_historical_reference':after,
    'product_requests_avoided_in_replay':before['planned_product_requests']-after['planned_product_requests'],
    'catalog_requests_avoided':0,'full_197_house_two_hour_goal_achieved':False,'network_requests':0}
core.write_json('outputs/ozon_fresh_fast/optimization_benchmark.json',report)
print(json.dumps(report,ensure_ascii=True))
