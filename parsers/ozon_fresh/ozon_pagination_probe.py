"""Small opt-in public page-number probe inside the sole address coordinator."""
from pathlib import Path
import json,re
from urllib.parse import urlsplit,urlunsplit,urlencode,parse_qs
import ozon_fresh_fast as core
import ozon_har_catalog as tiles


def run_queued_probe(output,zone,categories,client,location,state):
    request=Path(output)/'pagination_probe_request.json'
    if not request.exists():return
    job=core.load_json(request)
    if job.get('h3_index')!=zone['h3_index']:return
    identifier=job.get('id','')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',identifier):return
    folder=Path(output)/'pagination_probes'/identifier
    if (folder/'report.json').exists():return
    category=next((c for c in categories if c['category_id']==job.get('category_id')),None)
    pages=job.get('pages',[])
    if not category or not isinstance(pages,list) or not 1<=len(pages)<=5:return
    if any(type(page) is not int or not 1<=page<=1000 for page in pages):return
    report={'complete':False,'h3_index':zone['h3_index'],'category_id':category['category_id'],
            'started_at':core.now(),'pages':[],'purpose':'Compare public numeric pagination with saved sequential pages'}
    try:
        location.confirm()
        expected={}
        for url,payload in state.db.execute('SELECT url,payload FROM pages WHERE cache_key=?',
                    (json.dumps([zone['h3_index'],category['category_id']]),)):
            page=int(parse_qs(urlsplit(url).query).get('page',['1'])[0])
            rows=json.loads(payload)['rows']
            if rows:expected.setdefault(page,set()).update(row['product_id'] for row in rows)
        base=urlsplit(category['url'])
        for number in pages:
            path=urlunsplit(('', '',base.path,urlencode({'miniapp':'supermarket','page':number}),''))
            data=client.entrypoint(path)
            core.write_json(folder/f'page_{number}.json',data)
            address=tiles.address_title(data)
            if address and tiles.normalized_address(address)!=tiles.normalized_address(zone['address']):
                raise core.ParseError('Probe address differs from selected house')
            grid=tiles.primary_catalog_grid(data,require_layout=True)
            skus={str(card['sku']) for card in grid['items']}
            report['pages'].append({'requested_page':number,'grid_page':grid.get('page'),
                'server_page':parse_qs(urlsplit(data.get('pageInfo',{}).get('url','')).query).get('page'),
                'items':len(grid['items']),'unique_skus':len(skus),'expected_skus':len(expected.get(number,[])),
                'same_skus_as_saved_sequential_page':skus==expected.get(number),
                'skus':sorted(skus)})
        location.confirm()
        report['complete']=True
    except core.ProtectionError:raise
    except core.ParseError as exc:report['error']=str(exc)
    finally:
        report['finished_at']=core.now()
        core.write_json(folder/'report.json',report)
