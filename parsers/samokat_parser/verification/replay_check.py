import pathlib,json,sys
root=pathlib.Path(__file__).resolve().parent.parent;sys.path.insert(0,str(root))
from samokat.catalog import Catalog,FOOD_SECTIONS
from samokat.core import load_zones
responses=[json.loads(p.read_text('utf-8')) for p in (root/'verification/captured_public_responses').glob('*.json')]
lookup={}
for d in responses:
 q=d['request'];key=json.dumps({k:q[k] for k in ('url','method','body','params') if k in q},sort_keys=True)
 lookup[key]=d['data']
cats=next(d['data'] for d in responses if d['request']['url'].endswith('/categories/list'))
rootnames={'Готовая еда','Овощи и фрукты','Молоко, яйца и сыр','Хлеб и выпечка','Мясо и рыба'}
allids={c['id'] for c in cats}
roots={c['id'] for c in cats if c['name'] in rootnames and c.get('parentId') not in allids}
ids=set(roots)
while True:
 new={c['id'] for c in cats if c.get('parentId') in ids}
 if new<=ids:break
 ids|=new
subset=[c for c in cats if c['id'] in ids and (c['id'] in roots or c.get('parentId') in ids)]
class Recorded:
 def __init__(self):self.calls=0
 def query(self,url,method='GET',body=None,params=None):
  self.calls+=1
  if url.endswith('/categories/list'):return subset
  q={'url':url,'method':method}
  if body is not None:q['body']=body
  if params is not None:q['params']=params
  key=json.dumps(q,sort_keys=True)
  if key not in lookup:raise RuntimeError('No recorded response: '+key)
  return lookup[key]
zone=load_zones(root/'data/hexagons.csv')[0]
api=Recorded();products=Catalog(api).get_catalog(zone['address'],zone['address_lat'],zone['address_lon'])
print('REAL_RESPONSE_REPLAY_OK',len(products),'products',api.calls,'queries; scope = first five food sections')
