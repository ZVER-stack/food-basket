"""Read-only per-house export and durable, separately stored product metadata."""
import argparse
import collections
import csv
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

FIELDS = ['product_id','price_card','price','price_regular','amount','brand',
          'description','kcal','protein','fat','carbs']
HEADERS = ['Код гексагона','Адрес дома','ID товара','Цена с Ozon Картой',
           'Цена без Ozon Карты','Цена без скидки','Вес','Бренд','Описание',
           'Калории','Белки','Жиры','Углеводы']
STATIC = ('brand','description','kcal','protein','fat','carbs')
REQUIRED_METADATA = ('brand','description','kcal','protein','fat','carbs')

def is_mass(value):
    return bool(re.fullmatch(r'\s*\d+(?:[.,]\d+)?\s*(?:г|кг|мг)\s*',str(value or ''),re.I))

def present(value):
    return value is not None and value != ''

def extra(row):
    value = row.get('extra') or {}
    return json.loads(value) if isinstance(value,str) else value

def identity(row):
    name = re.sub(r'\s+',' ',str(row.get('name') or '')).strip().casefold().replace('ё','е')
    amount = re.sub(r'\s+','',str(row.get('amount') or '')).casefold().replace(',','.')
    return json.dumps([str(row.get('product_id') or ''),name,amount],ensure_ascii=False)

@contextmanager
def read_only(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:yield db
    finally:db.close()

def house_rows(snapshot):
    """Yield deduplicated house rows, including only the current partial house."""
    snapshot = Path(snapshot)
    with read_only(snapshot/'snapshot.sqlite') as db:
        completed = set()
        for h3,stamp,payload in db.execute("SELECT h3,parsed_at,payload FROM zones WHERE status IN ('complete','collected_with_gaps') ORDER BY rowid"):
            completed.add(h3)
            yield h3,stamp,deduplicate(json.loads(payload),stamp)
        progress_path = snapshot/'progress.json'
        progress = json.loads(progress_path.read_text(encoding='utf-8-sig')) if progress_path.exists() else {}
        h3 = progress.get('h3_index')
        if h3 and h3 not in completed:
            rows=[];stamp=''
            prefix=json.dumps([h3])[:-1]+','
            for stamp,payload in db.execute('SELECT captured_at,payload FROM pages WHERE cache_key>=? AND cache_key<? ORDER BY captured_at',(prefix,prefix[:-1]+'-')):
                rows.extend(json.loads(payload)['rows'])
            if rows:yield h3,stamp,deduplicate(rows,stamp)

def deduplicate(rows,stamp):
    result={}
    for row in rows:
        sku=str(row.get('product_id') or '')
        if not sku.isdigit():continue
        observed=str(extra(row).get('observation_received_at') or row.get('parsed_at') or stamp)
        if sku not in result or observed>result[sku][0]:result[sku]=(observed,row)
    return [value[1] for value in result.values()]

class MetadataStore:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path,timeout=60)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS products (identity TEXT PRIMARY KEY, sku TEXT, metadata TEXT, sources TEXT, status TEXT, attempts TEXT, checked_at TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS job_meta (key TEXT PRIMARY KEY,value TEXT)')

    def entries(self):
        return {key:{'sku':sku,'metadata':json.loads(metadata),'sources':json.loads(sources),
                     'status':status,'attempts':json.loads(attempts),'checked_at':checked}
                for key,sku,metadata,sources,status,attempts,checked in self.db.execute('SELECT * FROM products')}

    def save(self,key,entry):
        self.db.execute('INSERT OR REPLACE INTO products VALUES (?,?,?,?,?,?,?)',
            (key,entry['sku'],json.dumps(entry['metadata'],ensure_ascii=False),
             json.dumps(entry['sources'],ensure_ascii=False),entry['status'],
             json.dumps(entry['attempts'],ensure_ascii=False),entry.get('checked_at')))

    def seed(self,snapshot):
        entries=self.entries();houses=rows_read=0
        for h3,stamp,rows in house_rows(snapshot):
            houses+=1;rows_read+=len(rows)
            for row in rows:
                key=identity(row)
                entry=entries.setdefault(key,{'sku':str(row['product_id']),'metadata':{},'sources':[],
                    'status':'pending','attempts':[],'checked_at':None})
                observed=str(extra(row).get('product_metadata_observed_at') or row.get('parsed_at') or stamp)
                for field in STATIC:
                    if present(row.get(field)):
                        old=entry['metadata'].get(field)
                        if old is None or observed>old['at']:
                            entry['metadata'][field]={'value':row[field],'at':observed,'h3':h3,'source':'saved_product'}
                # Keep three known houses for a genuinely unavailable product,
                # not three retries of the same request at the same house.
                minimal={k:row.get(k) for k in ('product_id','name','amount','url')}
                entry['sources']=[s for s in entry['sources'] if s['h3']!=h3]
                entry['sources'].append({'h3':h3,'row':minimal})
                entry['sources']=entry['sources'][-3:]
                if is_mass(row.get('amount')):
                    entry['metadata']['weight']={'value':row['amount'],'at':observed,'h3':h3,'source':'saved_declared_mass'}
                if entry['status'] in ('pending','saved_complete'):
                    complete=all(present(entry['metadata'].get(f,{}).get('value')) for f in (*REQUIRED_METADATA,'weight'))
                    entry['status']='saved_complete' if complete else 'pending'
        for key,entry in entries.items():self.save(key,entry)
        self.db.execute('INSERT OR REPLACE INTO job_meta VALUES (?,?)',('seed',json.dumps({'houses':houses,'house_products':rows_read,'source':str(Path(snapshot).resolve())})))
        self.db.commit()
        return {'houses':houses,'house_products':rows_read,'identities':len(entries),
                'pending':sum(e['status']=='pending' for e in entries.values())}

    def close(self):self.db.close()

def merge_row(row,entry):
    """Prices, house and availability never come from a metadata source."""
    result={k:row.get(k) for k in FIELDS}
    if entry:
        for field in STATIC:
            value=entry['metadata'].get(field,{}).get('value')
            if present(value):result[field]=value
        weight=entry['metadata'].get('weight',{}).get('value')
        if present(weight):result['amount']=weight
    if not is_mass(result.get('amount')):
        result['amount']=''
    return result

def export(snapshot,zones_file,store_path,output):
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    with Path(zones_file).open(encoding='utf-8-sig',newline='') as stream:
        addresses={z['h3_index']:z['address'] for z in csv.DictReader(stream)}
    store=MetadataStore(store_path);entries=store.entries();store.close()
    rows_written=0;skus=set();houses=[];missing=collections.Counter()
    temp=output.with_suffix('.csv.tmp')
    with temp.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(HEADERS)
        for h3,stamp,rows in house_rows(snapshot):
            if h3 not in addresses:raise ValueError('Saved house has no verified address mapping: '+h3)
            houses.append(h3)
            for row in rows:
                if row.get('h3_index') not in (None,'',h3):raise ValueError('Saved row belongs to another house')
                result=merge_row(row,entries.get(identity(row)))
                writer.writerow([h3,addresses[h3]]+[result.get(k) if result.get(k) is not None else '' for k in FIELDS])
                for field in FIELDS:
                    if not present(result.get(field)):missing[field]+=1
                rows_written+=1;skus.add(str(row['product_id']))
    temp.replace(output)
    report={'columns':len(HEADERS),'house_product_rows':rows_written,'unique_products':len(skus),
            'houses_including_partial':len(houses),'missing':dict(missing),
            'metadata_statuses':dict(collections.Counter(e['status'] for e in entries.values())),
            'source_database_unchanged':True,'price_policy':'all three prices from one saved observation of this exact house',
            'metadata_policy':'same SKU, normalized name and declared amount; missing values never invented',
            'csv':str(output.resolve())}
    output.with_suffix('.coverage.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',required=True);parser.add_argument('--zones',required=True)
    parser.add_argument('--store',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--seed',action='store_true');args=parser.parse_args()
    if args.seed:
        store=MetadataStore(args.store)
        try:print(json.dumps(store.seed(args.snapshot),ensure_ascii=True),flush=True)
        finally:store.close()
    print(json.dumps(export(args.snapshot,args.zones,args.store,args.output),ensure_ascii=True),flush=True)

if __name__=='__main__':main()
