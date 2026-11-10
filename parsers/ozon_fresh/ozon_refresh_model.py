"""Pure selective-refresh observations; historical absence is never inferred."""
from copy import deepcopy
from datetime import datetime
import json
import re

import ozon_fresh_fast as core
from ozon_catalog_index import (checkpoint_quote, extra_of, matching_quote,
                                merge_observation, remember_quote, reuse_quote)


class RefreshAccumulator:
    def __init__(self, zone, baseline_rows, baseline_id):
        self.zone = deepcopy(zone)
        self.baseline_id = str(baseline_id)
        values = baseline_rows.values() if isinstance(baseline_rows, dict) else baseline_rows
        self.baseline = {str(row['product_id']): checkpoint_quote(deepcopy(row)) for row in values}
        self.observations, self.pending, self.seen = {}, [], set()
        self._unresolved_stamps = {}

    @staticmethod
    def _stamp(stamp):
        try:
            if not isinstance(stamp, str) or not re.fullmatch(
                    r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?', stamp):
                raise ValueError
            parsed = datetime.fromisoformat(stamp)
        except ValueError as exc:
            raise core.ParseError('Refresh observation requires an actual seconds/microseconds timestamp') from exc
        return parsed.isoformat(sep=' ', timespec='microseconds')

    def _mark_unresolved(self, sku, stamp):
        latest = max(stamp, self._unresolved_stamps.get(sku, stamp))
        self._unresolved_stamps[sku] = latest
        previous = self.observations.get(sku)
        if previous:
            observed = self._stamp(extra_of(previous).get('observation_received_at') or previous['parsed_at'])
            if observed <= latest:
                # A later visible change is not a currently confirmed ordinary
                # quote. Preserve the baseline, suppress the older current row.
                del self.observations[sku]

    @staticmethod
    def _sku(item):
        sku = str(item.get('product_id', ''))
        if not re.fullmatch(r'\d+', sku):
            raise core.ParseError('Refresh observation has no numeric SKU')
        if extra_of(item).get('fresh_action_verified') is not True:
            raise core.ParseError('Refresh observation is not a verified Fresh tile')
        return sku

    def observe(self, category, item, stamp):
        stamp = self._stamp(stamp)
        sku = self._sku(item)
        self.seen.add(sku)
        item = remember_quote(item)
        item['extra']['catalog_quote_observed_at'] = stamp
        previous = self.baseline.get(sku)
        normalize = lambda name: re.sub(r'\s+', ' ', str(name or '')).strip().casefold()
        if (not item.get('amount') and previous and previous.get('amount') and
                normalize(item.get('name')) == normalize(previous.get('name'))):
            item['amount'] = previous['amount']
            item['extra']['amount_origin'] = {
                'source': 'same_SKU_metadata_reference',
                'snapshot_id': self.baseline_id,
                'h3_index': previous.get('h3_index'),
                'parsed_at': previous.get('parsed_at')}
        if item.get('amount') and item.get('price') is not None and not item.get('unit_price_basis'):
            self.accept(category, item, stamp)
            return {'needs_product': False, 'item': item}
        if (previous and item.get('amount') and matching_quote(item, previous, cross_address=True)):
            source_house = previous.get('h3_index') or self.zone['h3_index']
            item = reuse_quote(item, previous, self.zone['h3_index'], source_house)
            extra = extra_of(item)
            origin = deepcopy(extra['ordinary_price_origin'])
            origin.setdefault('snapshot_id', self.baseline_id)
            extra.update(ordinary_price_origin=origin,
                         ordinary_price_source='historical_matching_catalog_cache',
                         ordinary_price_inferred=True,
                         ordinary_price_verified_for_address=False,
                         inferred_price_fields=['price', 'price_regular', 'price_card',
                                                'unit_price', 'unit_price_basis']
                            if item.get('unit_price_basis') else ['price'])
            item['extra'] = extra
            self.accept(category, item, stamp)
            return {'needs_product': False, 'item': item}
        self._mark_unresolved(sku, stamp)
        return {'needs_product': True, 'item': item}

    def accept(self, category, item, stamp):
        stamp = self._stamp(stamp)
        sku = self._sku(item)
        self.seen.add(sku)
        item = deepcopy(item)
        extra = extra_of(item)
        quote = extra.get('catalog_quote', {})
        verified = {key for key in ('name', 'amount', 'in_stock', 'price', 'price_regular',
                                    'price_card', 'unit_price', 'unit_price_basis')
                    if quote.get(key) is not None}
        verified.add('product_id')
        if (extra.get('amount_origin') or {}).get('source') == 'same_SKU_metadata_reference':
            verified.discard('amount')
        if extra.get('ordinary_price_source') == 'product_webPrice':
            verified.update(('price', 'price_regular', 'price_card'))
            if (extra.get('amount_origin') or {}).get('source') in (
                    'product_characteristics', 'matching_product_title'):
                verified.add('amount')
        if extra.get('ordinary_price_inferred'):
            verified.difference_update(extra.get('inferred_price_fields') or ['price'])
            extra['ordinary_price_verified_for_address'] = False
        extra.update(refresh_mode='selective', full_zone_complete=False,
                     catalog_fully_checked=False, observation_received_at=stamp,
                     historical_baseline_id=self.baseline_id,
                     delivery_address=self.zone.get('address'), verified_fields=sorted(verified))
        item['extra'] = extra
        mapping = {field: '/' + field for field in core.COLUMNS
                   if field not in ('service', 'h3_index', 'parsed_at', 'category')}
        row = core.card_row(item, mapping, category)
        row.update(service='ozon_fresh', h3_index=self.zone['h3_index'], parsed_at=stamp[:19])
        row = {field: row.get(field) for field in core.COLUMNS}
        if any(row.get(field) is None or not str(row[field]).strip() for field in core.REQUIRED):
            raise core.ParseError('Refresh row lacks a mandatory field')
        if self._unresolved_stamps.get(sku, stamp) > stamp:
            return None
        self._unresolved_stamps.pop(sku, None)
        self.observations[sku] = merge_observation(self.observations.get(sku), row)
        self.pending[:] = [entry for entry in self.pending
                           if entry['product_id'] != sku or entry['observed_at'] > stamp]
        return self.observations[sku]

    def defer(self, category, item, stamp, reason):
        stamp = self._stamp(stamp)
        sku = self._sku(item)
        self.seen.add(sku)
        self._mark_unresolved(sku, stamp)
        self.pending.append({'product_id': sku, 'category': deepcopy(category),
                             'item': deepcopy(item), 'observed_at': stamp, 'reason': str(reason)})

    def summary(self):
        return {'seen_skus': sorted(self.seen), 'baseline_skus_count': len(self.baseline),
                'unchecked_baseline_skus': sorted(set(self.baseline) - set(self.observations)),
                'checked_products': len(self.observations), 'pending_count': len(self.pending),
                'removed_confirmed': 0, 'refresh_mode': 'selective',
                'full_zone_complete': False, 'catalog_fully_checked': False}
