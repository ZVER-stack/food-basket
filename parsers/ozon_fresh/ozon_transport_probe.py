"""One opt-in comparison of public catalog transports in the coordinator."""
from pathlib import Path
import re
from urllib.parse import quote,urlsplit,urlunsplit
import ozon_fresh_fast as core
import ozon_har_catalog as tiles


def run_queued_transport_probe(output,zone,categories,client,location):
    request=Path(output)/'transport_probe_request.json'
    if not request.exists():return
    job=core.load_json(request)
    if job.get('h3_index')!=zone['h3_index']:return
    identifier=job.get('id','')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',identifier):return
    category=next((c for c in categories if c['category_id']==job.get('category_id')),None)
    if not category:return
    folder=Path(output)/'transport_probes'/identifier
    if (folder/'report.json').exists():return
    parts=urlsplit(category['url'])
    relative=urlunsplit(('','',parts.path,parts.query,''))
    report={'complete':False,'started_at':core.now(),'h3_index':zone['h3_index'],
        'category_id':category['category_id'],'cases':[],
        'same_profile':True,'catalog_replacement_enabled':False}
    location.confirm()
    try:
        for label,root in (('entrypoint','entrypoint-api.bx'),('composer','composer-api.bx')):
            case={'transport':label,'accepted_same_house_fresh_catalog':False}
            try:
                data=client.request({'url':core.HOST+'/api/'+root+'/page/json/v2?url='+quote(relative,safe='')})
                core.write_json(folder/(label+'.json'),data)
                address=tiles.address_title(data)
                if not address or tiles.normalized_address(address)!=tiles.normalized_address(zone['address']):
                    raise core.ParseError('Comparison response does not confirm the requested house')
                grid=tiles.primary_catalog_grid(data,require_layout=True)
                products=[tiles.tile_product(card) for card in grid['items']]
                case.update(accepted_same_house_fresh_catalog=True,items=len(products),
                    sku_ids=sorted({p['product_id'] for p in products}),
                    missing_ordinary_prices=sum(p.get('price') is None for p in products))
            except core.ProtectionError:raise
            except core.ParseError as exc:
                case['error']=str(exc)
            report['cases'].append(case)
            if not case['accepted_same_house_fresh_catalog'] and label=='entrypoint':break
        location.confirm()
        report['complete']=True
    finally:
        report['finished_at']=core.now()
        core.write_json(folder/'report.json',report)
