"""SKU reference and address-specific offers; no basket or weight arithmetic.

Cross-address price reuse is an explicitly selected research approximation.
Only complete, address-confirmed catalogs seed it. Original price provenance
survives every reuse, and the result never claims destination price verification.
"""
from copy import deepcopy
from decimal import Decimal
import json
import re

import ozon_fresh_fast as core

PRICE_FIELDS = ('price', 'price_regular', 'price_card')
METADATA_FIELDS = ('brand', 'manufacturer', 'country', 'kcal', 'protein', 'fat', 'carbs', 'composition', 'description', 'shelf_life', 'storage_conditions')

OFFER_FIELDS = PRICE_FIELDS + ('in_stock', 'amount', 'unit_price', 'unit_price_basis')


def extra_of(row):
    extra = row.get('extra') or {}
    return json.loads(extra) if isinstance(extra, str) else deepcopy(extra)


def signature(item):
    """Capture the catalog quote BEFORE replacing any fields from a PDP/cache."""
    extra = extra_of(item)
    return {
        'name': re.sub(r'\s+', ' ', item.get('name') or '').strip().casefold(),
        'amount': item.get('amount'), 'in_stock': item.get('in_stock'),
        'price_style': extra.get('price_style'),
        **{key: item.get(key) for key in PRICE_FIELDS},
        'unit_price': item.get('unit_price'),
        'unit_price_basis': item.get('unit_price_basis'),
    }


def remember_quote(item):
    result = deepcopy(item)
    extra = extra_of(item)
    extra['catalog_quote'] = signature(item)
    if item.get('price') is not None and not item.get('unit_price_basis'):
        extra.update(ordinary_price_source='catalog_explicit_ordinary',
                     ordinary_price_verified_for_address=True, ordinary_price_inferred=False)
    result['extra'] = extra
    return result


def checkpoint_quote(row):
    """Conservatively recover a non-weighted older quote for the reference.

    Earlier checkpoints retained divergent catalog values in catalog_prices.
    When they agree, explicit same-house PDP card/list prices can anchor the
    user-authorized comparison too. A weighted quote is never reconstructed.
    """
    extra = extra_of(row)
    if extra.get('catalog_quote'):
        return row
    if row.get('unit_price_basis') or extra.get('price_basis'):
        return row
    style = extra.get('price_style')
    if style not in ('CARD_PRICE', 'ACTUAL_PRICE', 'SALE_PRICE'):
        return row
    item = deepcopy(row)
    if 'catalog_prices' in extra:
        item.update(extra['catalog_prices'])
    elif style == 'CARD_PRICE':
        if extra.get('ordinary_price_source') != 'product_webPrice':
            return row
        item['price'] = None
    extra['catalog_quote'] = signature(item)
    extra['catalog_quote_recovered_from_checkpoint'] = True
    result = deepcopy(row)
    result['extra'] = json.dumps(extra, ensure_ascii=False)
    return result


def needs_product(item):
    if extra_of(item).get('product_details_required'):
        return True
    if extra_of(item).get('price_cache_match'):
        return False
    return not item.get('amount') or item.get('price') is None or bool(item.get('unit_price_basis'))


def matching_quote(item, previous, cross_address=False):
    before = extra_of(previous).get('catalog_quote')
    current = extra_of(item).get('catalog_quote') or signature(item)
    if not before or before != current or previous.get('price') is None:
        return False
    if (not current.get('unit_price_basis') and current.get('price_regular') is not None and
            Decimal(str(previous['price'])) > Decimal(str(current['price_regular']))):
        return False
    # No cross-house inference from a single card price, a missing list price,
    # an unavailable offer, or an unrelated marketplace seller.
    if cross_address and (current.get('price_card') is None or
                          current.get('price_regular') is None or
                          str(current.get('in_stock')).lower() != 'true' or
                          not extra_of(item).get('fresh_action_verified')):
        return False
    return True


def reuse_quote(item, previous, target_house, source_house):
    result = deepcopy(item)
    extra, old = extra_of(result), extra_of(previous)
    for key in PRICE_FIELDS + ('unit_price', 'unit_price_basis'):
        result[key] = previous.get(key)
    # For a normal pack the list/card prices are explicitly visible NOW at the
    # destination. Only the missing ordinary price needs the approximation.
    # Weighted tiles can show per-kg prices, so retain the source's literal pack
    # quote as a group there instead of mixing incompatible bases.
    if not item.get('unit_price_basis'):
        result['price_regular'] = item.get('price_regular') or previous['price_regular']
        result['price_card'] = item.get('price_card')
    origin = old.get('ordinary_price_origin') or {
        'h3_index': source_house,
        'parsed_at': old.get('ordinary_price_observed_at') or previous.get('parsed_at'),
        'source': old.get('ordinary_price_source', 'same_house_explicit_offer'),
    }
    inferred = origin['h3_index'] != target_house
    extra.update(ordinary_price_origin=origin,
                 ordinary_price_source='matching_catalog_cache' if inferred else 'same_house_offer_cache',
                 ordinary_price_verified_for_address=not inferred,
                 ordinary_price_inferred=inferred,
                 inferred_price_fields=list(PRICE_FIELDS) if inferred and item.get('unit_price_basis') else (['price'] if inferred else []),
                 visible_price_fields_source='current_address_catalog',
                 reused_product_prices={key:previous.get(key) for key in PRICE_FIELDS},
                 price_cache_match='SKU, name, amount, stock, style and all displayed catalog prices',
                 price_reused_from_h3=source_house)
    result['extra'] = extra
    return result


def merge_observation(previous, current):
    """Update later quotes as a unit and keep history; never average prices."""
    if not previous:
        return current
    if previous['product_id'] != current['product_id']:
        raise core.ParseError('Cannot merge different SKUs')
    same_offer = not any(previous.get(key) != current.get(key) for key in OFFER_FIELDS)
    older, newer = previous, current
    a = extra_of(previous).get('observation_received_at') or previous.get('parsed_at')
    b = extra_of(current).get('observation_received_at') or current.get('parsed_at')
    if not a or not b or a == b:
        if not same_offer:
            raise core.ParseError(f"Conflicting simultaneous offers for SKU {current['product_id']}")
        # Equal prices do not make provenance interchangeable. A same-house
        # cache hit can still carry an inferred price from another house.
        def verified_here(row):
            extra = extra_of(row)
            if (extra.get('ordinary_price_verified_for_address') is not True or
                    extra.get('ordinary_price_inferred') is not False):
                return False
            origin_house = (extra.get('ordinary_price_origin') or {}).get('h3_index')
            if origin_house and row.get('h3_index') and origin_house != row['h3_index']:
                return False
            if (extra.get('ordinary_price_source') == 'same_house_offer_cache' and
                    origin_house and not row.get('h3_index')):
                return False
            return True
        previous_verified, current_verified = verified_here(previous), verified_here(current)
        if previous_verified and not current_verified:
            older, newer = current, previous
        elif previous_verified == current_verified and bool(a) > bool(b):
            older, newer = current, previous
    elif a > b:
        older, newer = current, previous
    result = deepcopy(newer)
    extra = extra_of(newer)
    history = extra_of(older).get('earlier_observations', []) + extra.get('earlier_observations', [])
    observation = {key: older.get(key) for key in ('parsed_at',) + OFFER_FIELDS}
    provenance = extra_of(older)
    observation['price_provenance'] = {key: provenance[key] for key in (
        'ordinary_price_source', 'ordinary_price_origin', 'ordinary_price_observed_at',
        'ordinary_price_verified_for_address', 'ordinary_price_inferred',
        'catalog_quote', 'catalog_quote_observed_at', 'observation_received_at') if key in provenance}
    history.append(observation)
    # Cached page replay can encounter the same historical observation twice.
    extra['earlier_observations'] = list({json.dumps(x, sort_keys=True): x for x in history}.values())
    extra['offer_selection'] = ('latest_observation_at_this_address' if a and b and a != b
                                else 'equal_offer_verified_provenance_preferred')
    result['extra'] = json.dumps(extra, ensure_ascii=False)
    return result


class CatalogIndex:
    def __init__(self, state, allow_price_reuse=False, reference_snapshot=None, metadata_strategy=False, metadata_store=None):
        self.state = state
        self.metadata_strategy = metadata_strategy
        self.metadata_reference = {}
        self.backfill_metadata = {}
        self.allow_price_reuse = allow_price_reuse
        self.reference = {}
        self.products = {}
        self.product_houses = {}
        self.checkpoint_quotes = {}
        self.historical_keys = {}
        self.reference_snapshot = None
        self.stats = {'same_house_cache_hits': 0, 'cross_house_cache_hits': 0,
                      'historical_cache_hits':0,'imported_reference_houses':0,
                      'product_requests_needed': 0, 'catalog_prices_used': 0,
                      'metadata_reused':0, 'new_or_changed_product_requests':0,
                      'backfill_entries_loaded':0,'backfill_metadata_reused':0,'backfill_card_reads_avoided':0}
        if reference_snapshot:self.load_reference(reference_snapshot)
        # Completed zone payloads are already transactionally durable. Partial
        # pages cannot turn into a completed baseline by constructing this index.
        for house, payload in state.db.execute("SELECT h3,payload FROM zones WHERE status IN ('complete','collected_with_gaps') ORDER BY parsed_at,h3"):
            self.publish(house, json.loads(payload))
        if metadata_store and metadata_strategy:self.load_metadata_store(metadata_store)

    def load_metadata_store(self,path):
        """Read static fields only; the sidecar never supplies destination offers."""
        from pathlib import Path
        from ozon_house_export import read_only, STATIC
        path=Path(path).resolve()
        if not path.is_file():raise core.ParseError('Configured metadata store is missing')
        own=Path(self.state.db.execute('PRAGMA database_list').fetchone()[2]).resolve()
        with read_only(path) as db:
            db.execute('PRAGMA query_only=ON')
            seed=db.execute("SELECT value FROM job_meta WHERE key='seed'").fetchone()
            if not seed or Path(json.loads(seed[0])['source']).resolve()!=own.parent:
                raise core.ParseError('Metadata store belongs to another snapshot')
            for key,sku,raw,status,checked in db.execute('SELECT identity,sku,metadata,status,checked_at FROM products'):
                if status not in ('checked_complete','checked_missing_on_site','saved_complete'):continue
                identity=json.loads(key)
                if (not isinstance(identity,list) or len(identity)!=3 or
                        not all(isinstance(v,str) for v in identity) or identity[0]!=str(sku) or not str(sku).isdigit()):
                    raise core.ParseError('Metadata store identity and SKU disagree')
                metadata=json.loads(raw)
                if status=='saved_complete' and not all(metadata.get(f,{}).get('value') not in (None,'') for f in (*STATIC,'weight')):continue
                if status.startswith('checked_') and not checked:continue
                fields={}
                for field in STATIC:
                    value=metadata.get(field,{})
                    if value.get('value') not in (None,''):
                        value=deepcopy(value)
                        if field in ('kcal','protein','fat','carbs'):value['value']=core.nutrition_number(value['value'])
                        fields[field]=value
                stamp=checked or max((v.get('at','') for v in fields.values()),default='')
                if not stamp:continue
                self.backfill_metadata[key]={'fields':fields,'checked_at':stamp,'store':str(path)}
        self.stats['backfill_entries_loaded']=len(self.backfill_metadata)

    def load_reference(self,path):
        """Read only completed houses from an older, possibly partial snapshot.

        Reference rows are metadata/price candidates, never output or evidence
        of the new address's assortment. A full197-house baseline is unnecessary.
        """
        import sqlite3
        from pathlib import Path
        from contextlib import closing
        path=Path(path).resolve()
        if path.is_dir():path=path/'snapshot.sqlite'
        own=self.state.db.execute('PRAGMA database_list').fetchone()[2]
        if own and Path(own).resolve()==path:return
        if not path.is_file():raise core.ParseError('Reference snapshot.sqlite is missing')
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as source:
            source.execute('PRAGMA query_only=ON')
            expected=self.state.db.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()[0]
            actual=source.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
            if not actual or actual[0]!=expected:raise core.ParseError('Reference fingerprint differs from current inputs')
            for house,stamp,payload in source.execute(
                    "SELECT h3,parsed_at,payload FROM zones WHERE status IN ('complete','collected_with_gaps') ORDER BY parsed_at,h3"):
                rows=json.loads(payload)
                if not isinstance(rows,list):raise core.ParseError('Invalid reference house rows')
                for row in rows:
                    row.setdefault('parsed_at',stamp)
                    for key,value in (('parsed_at',stamp),('h3_index',house),('service','ozon_fresh')):
                        if row.get(key) in (None,''):row[key]=value
                    if any(row.get(k) in (None,'') for k in core.REQUIRED):
                        raise core.ParseError('Reference row lacks a mandatory field')
                    extra=extra_of(row)
                    if (row['service']!='ozon_fresh' or row['h3_index']!=house or
                        not re.fullmatch(r'\d+',str(row['product_id'])) or
                        extra.get('fresh_action_verified') is not True):
                        raise core.ParseError('Reference service, house, SKU or Fresh provenance is invalid')
                    if row.get('url') and not re.search(r'/product/(?:[^/?]*-)?'+re.escape(str(row['product_id']))+r'/?(?:\?|$)',row['url']):
                        raise core.ParseError('Reference URL and SKU disagree')
                self.publish(house,rows,historical_snapshot=str(path))
                self.stats['imported_reference_houses']+=1
        self.reference_snapshot=str(path)

    def remember_product(self, house, row):
        if extra_of(row).get('product_metadata_observed_at') and extra_of(row).get('product_metadata_schema') == 2:
            self.metadata_reference[row['product_id']] = (house, deepcopy(row))

    def publish(self, house, rows, historical_snapshot=None):
        for row in rows:
            self.remember_product(house, row)
            row = checkpoint_quote(row)
            self.products[row['product_id']] = deepcopy(row)
            self.product_houses[row['product_id']] = house
            extra = extra_of(row)
            quote = extra.get('catalog_quote')
            if not quote or row.get('price') is None or extra.get('ordinary_price_inferred'):
                continue
            if historical_snapshot and (extra.get('ordinary_price_verified_for_address') is not True or
                    (extra.get('ordinary_price_origin') or {}).get('h3_index',house)!=house):
                continue
            key = (row['product_id'], json.dumps(quote, sort_keys=True))
            self.reference[key] = (house, deepcopy(row))
            if historical_snapshot:self.historical_keys[key]=historical_snapshot
            else:self.historical_keys.pop(key,None)

    def resolve(self, house, item, local):
        item = remember_quote(item)
        from ozon_house_export import identity as metadata_identity
        backfill=self.backfill_metadata.get(metadata_identity(item)) if self.metadata_strategy else None
        if self.metadata_strategy:
            source = self.metadata_reference.get(item['product_id'])
            known = local.get(item['product_id'])
            if known and extra_of(known).get('product_metadata_observed_at') and extra_of(known).get('product_metadata_schema') == 2:
                source = (house, known)
            required = True
            if source:
                source_house, previous = source
                old = extra_of(previous)
                same_identity = (signature(item)['name'] == signature(previous)['name'] and
                    (not item.get('amount') or not previous.get('amount') or item['amount'] == previous['amount']))
                if same_identity:
                    for field in METADATA_FIELDS:
                        if previous.get(field) not in (None, ''): item[field] = deepcopy(previous[field])
                    if not item.get('amount') and previous.get('amount'): item['amount'] = previous['amount']
                    for key in ('product_characteristics', 'product_metadata_observed_at', 'product_metadata_schema'):
                        if key in old: item['extra'][key] = deepcopy(old[key])
                    item['extra']['metadata_origin'] = {'h3_index':source_house,
                        'observed_at':old.get('product_metadata_observed_at'), 'sku':item['product_id']}
                    self.stats['metadata_reused'] += 1
                    before = old.get('catalog_quote')
                    current = item['extra']['catalog_quote']
                    required = not before or before != current
            if backfill:
                observed=item['extra'].get('product_metadata_observed_at','')
                for field,value in backfill['fields'].items():
                    if item.get(field) in (None,'') or value.get('at','')>=observed:
                        item[field]=deepcopy(value['value'])
                # A checked missing value is unknown, not a reason to read
                # the same unchanged card at every house. Price decisions
                # still require a separate, exact saved catalog quote.
                previous=known or (source[1] if source else self.products.get(item['product_id']))
                if (previous and metadata_identity(previous)==metadata_identity(item) and
                        extra_of(previous).get('catalog_quote')==item['extra']['catalog_quote']):
                    if required:self.stats['backfill_card_reads_avoided']+=1
                    required=False
                item['extra']['backfill_metadata_origin']={
                    'store':backfill['store'],'identity':metadata_identity(item),
                    'checked_at':backfill['checked_at'],'fields':deepcopy(backfill['fields'])}
                self.stats['backfill_metadata_reused']+=1
            item['extra']['product_details_required'] = required
            if required: self.stats['new_or_changed_product_requests'] += 1
        metadata = local.get(item['product_id']) or self.products.get(item['product_id'])
        if (not item.get('amount') and metadata and metadata.get('amount') and
                signature(item)['name']==signature(metadata)['name']):
            item['amount']=metadata['amount']
            item['extra']['amount_origin']={
                'source':'same_SKU_metadata_reference',
                'h3_index':house if item['product_id'] in local else self.product_houses[item['product_id']],
                'parsed_at':metadata.get('parsed_at')}
        if not needs_product(item):
            self.stats['catalog_prices_used'] += 1
            return item
        known = local.get(item['product_id'])
        if known and matching_quote(item, known):
            self.stats['same_house_cache_hits'] += 1
            return reuse_quote(item, known, house, house)
        # A resumed parallel walk may reach a new category before replaying a
        # previously committed category containing this same SKU. Use a matching
        # same-house checkpoint only as a price lookup; never as catalog output
        # or proof that any destination page has been traversed.
        if house not in self.checkpoint_quotes:
            cached = {}
            prefix=json.dumps([house])[:-1]+','
            # Prefix range uses the pages primary-key index and avoids reading
            # payloads from every other house in a large snapshot.
            for key,payload in self.state.db.execute(
                    'SELECT cache_key,payload FROM pages WHERE cache_key>=? AND cache_key<?',
                    (prefix,prefix[:-1]+'-')):
                for row in json.loads(payload)['rows']:
                    row=checkpoint_quote(row)
                    quote=extra_of(row).get('catalog_quote')
                    if not quote or row.get('price') is None:continue
                    lookup=(row['product_id'],json.dumps(quote,sort_keys=True))
                    previous=cached.get(lookup)
                    cached[lookup]=merge_observation(previous,row) if previous else row
            self.checkpoint_quotes={house:cached}
        lookup=(item['product_id'],json.dumps(extra_of(item)['catalog_quote'],sort_keys=True))
        committed=self.checkpoint_quotes[house].get(lookup)
        if committed and matching_quote(item,committed):
            if not item.get('amount') and committed.get('amount'):
                item=deepcopy(item)
                item['amount']=committed['amount']
                item['extra']['amount_origin']={'source':'same_SKU_same_house_checkpoint',
                    'h3_index':house,'parsed_at':committed.get('parsed_at')}
            self.stats['same_house_cache_hits']+=1
            return reuse_quote(item,committed,house,house)
        # Preserve the existing same-house optimization for older checkpoint
        # rows written before raw catalog signatures were recorded.
        if (known and not extra_of(known).get('catalog_quote') and
                not item.get('unit_price_basis') and known.get('price') is not None and
                item.get('price_card') is not None and known.get('price_card') == item['price_card'] and
                item.get('price_regular') is not None and known.get('price_regular') == item['price_regular'] and
                Decimal(str(known['price'])) <= Decimal(str(item['price_regular'])) and
                known.get('amount') == item.get('amount')):
            self.stats['same_house_cache_hits'] += 1
            return reuse_quote(item, known, house, house)
        if self.allow_price_reuse:
            key = (item['product_id'], json.dumps(extra_of(item)['catalog_quote'], sort_keys=True))
            source = self.reference.get(key)
            historical=self.historical_keys.get(key)
            if source and (source[0] != house or historical) and matching_quote(item, source[1], cross_address=True):
                if source[0]!=house:self.stats['cross_house_cache_hits'] += 1
                result=reuse_quote(item, source[1], house, source[0])
                if historical:
                    self.stats['historical_cache_hits']+=1
                    extra=extra_of(result)
                    origin=deepcopy(extra['ordinary_price_origin'])
                    origin.setdefault('snapshot_id',historical)
                    extra.update(ordinary_price_origin=origin,ordinary_price_inferred=True,
                        ordinary_price_verified_for_address=False,
                        ordinary_price_source='historical_matching_catalog_cache',
                        inferred_price_fields=list(PRICE_FIELDS)+['unit_price','unit_price_basis']
                            if item.get('unit_price_basis') else ['price'])
                    result['extra']=extra
                return result
        self.stats['product_requests_needed'] += 1
        return item

    def difference(self, rows):
        current = {row['product_id']: row for row in rows}
        baseline = self.products
        return {'new_skus': sorted(set(current) - set(baseline)),
                'absent_skus': sorted(set(baseline) - set(current)),
                'changed_catalog_quotes': sorted(sku for sku in set(current) & set(baseline)
                    if extra_of(current[sku]).get('catalog_quote') != extra_of(baseline[sku]).get('catalog_quote')),
                'baseline_skus': len(baseline), 'current_skus': len(current)}
