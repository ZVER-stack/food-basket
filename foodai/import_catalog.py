"""Local import of supervisor CSV or the collected ten-zone Ozon snapshot."""
import argparse
from collections import Counter
import csv
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from contextlib import closing

from foodai.models import jsonable, number
from foodai.services.catalog import ZONE_IDS
from foodai.services.units import parse_package


def convert(raw, service, zid, captured_at, price_mode):
    if zid not in ZONE_IDS:
        raise ValueError('unknown_zone')
    qty, unit = parse_package(raw.get('amount'))
    extra = raw.get('extra') or {}
    extra = json.loads(extra) if isinstance(extra, str) else extra
    if price_mode == 'ordinary' and extra.get('ordinary_price_inferred'):
        raise ValueError('ordinary_price_not_verified')
    price = raw.get('price_card') if price_mode == 'card' else raw.get('price')
    price = number(price)
    if price <= 0:
        raise ValueError('missing_positive_price')
    if not captured_at:
        raise ValueError('missing_timestamp')
    datetime.fromisoformat(captured_at)
    if not raw.get('product_id') or not str(raw.get('name') or '').strip():
        raise ValueError('missing_identity')
    # An unknown warehouse is represented as an address-specific storefront,
    # never equated with another address or asserted to be a warehouse ID.
    store = (f'{service}:{raw["store_id"]}' if raw.get('store_id') else f'{service}:address:{zid}')
    available = {'true': True, 'false': False}.get(str(raw.get('in_stock')).lower())
    return {'sku': str(raw['product_id']), 'service': service, 'storefront_id': store,
            'zone_id': zid, 'name': raw['name'], 'brand': raw.get('brand') or '',
            'category': raw.get('category') or '', 'package_qty': str(qty), 'package_unit': unit,
            'price_rub': str(price), 'available': available, 'captured_at': captured_at,
            'product_url': raw.get('url') or '', 'nutrition_per': 'unknown',
            'nutrition_source': 'unknown',
            **{k: str(raw[k]) if raw.get(k) is not None and raw.get(k) != '' else None
               for k in ('kcal', 'protein', 'fat', 'carbs')}}


def import_data(source, output, kind='snapshot', price_mode='ordinary', append=False):
    products, coverage, rejected = {}, {}, Counter()
    excluded = {}
    if append and Path(output).exists():
        previous = json.loads(Path(output).read_text(encoding='utf-8-sig'))
        if previous.get('demo') or previous.get('price_mode') != price_mode:
            raise ValueError('Дополнение требует реального каталога с тем же типом цены')
        products = {(p['zone_id'], p['storefront_id'], p['sku']): p for p in previous['products']}
        coverage = {(p['zone_id'], p['storefront_id']): p for p in previous['coverage']}
        excluded = {(p['zone_id'], p['sku']): p for p in previous.get('excluded_products', [])}
    def add(raw, service, zid, stamp, status):
        try:
            product = convert(raw, service, zid, stamp, price_mode)
        except (ValueError, TypeError, KeyError) as exc:
            rejected['invalid_or_unverified_offer'] += 1
            if str(exc) == 'ordinary_price_not_verified' and raw.get('product_id') and raw.get('name'):
                excluded[(zid, str(raw['product_id']))] = {
                    'zone_id': zid, 'sku': str(raw['product_id']), 'name': raw['name'],
                    'category': raw.get('category') or '', 'brand': raw.get('brand') or '',
                    'reason': 'unverified_ordinary_price'}
            return
        key = (product['zone_id'], product['storefront_id'], product['sku'])
        if key not in products or products[key]['captured_at'] < product['captured_at']:
            products[key] = product
        excluded.pop((zid, product['sku']), None)
        coverage[(zid, product['storefront_id'])] = {'zone_id': zid,
            'storefront_id': product['storefront_id'], 'service': service, 'status': status,
            'captured_at': stamp, 'price_mode': price_mode,
            'coverage_basis': 'representative_address_snapshot'}
    source = Path(source).resolve()
    if kind == 'snapshot':
        path = source/'snapshot.sqlite' if source.is_dir() else source
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            for zid, status, stamp, payload in db.execute('SELECT h3,status,parsed_at,payload FROM zones'):
                if zid not in ZONE_IDS:
                    raise ValueError('Этот снимок использует другие зоны. Нужен набор msk_center, msk_01–msk_09.')
                if status not in ('complete', 'collected_with_gaps'):
                    continue
                for raw in json.loads(payload):
                    add(raw, 'ozon_fresh', zid, raw.get('parsed_at') or stamp, status)
    else:
        with source.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            required = {'service', 'zone_id', 'product_id', 'name', 'price', 'amount', 'parsed_at'}
            if not required.issubset(reader.fieldnames or []):
                raise ValueError('Нужен CSV по общему шаблону с service, zone_id, product_id, name, price, amount, parsed_at.')
            for raw in reader:
                if raw['service'] not in ('lavka', 'samokat', 'vkusvill', 'ozon_fresh'):
                    raise ValueError('Неизвестный сервис')
                if not raw.get('product_id'):
                    continue
                add(raw, raw['service'], raw['zone_id'], raw['parsed_at'], 'collected_with_gaps')
    if not products:
        raise ValueError('Нет пригодных предложений. Проверьте зоны, фасовки, даты и тип цены.')
    data = {'demo': False, 'price_mode': price_mode, 'coverage': list(coverage.values()),
            'products': list(products.values()), 'import_report': dict(rejected),
            'excluded_products': list(excluded.values())}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix('.json.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf8')
    temp.replace(output)
    return {'products': len(products), 'zones': len({key[0] for key in products}),
            'rejected': dict(rejected), 'price_mode': price_mode}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--format', choices=('snapshot', 'csv'), default='snapshot')
    parser.add_argument('--price-mode', choices=('ordinary', 'card'), default='ordinary')
    parser.add_argument('--output', default='data/ozon.json')
    parser.add_argument('--append', action='store_true', help='Merge other service exports into this catalog')
    args = parser.parse_args()
    print(json.dumps(import_data(args.source, args.output, args.format, args.price_mode, args.append), ensure_ascii=False))


if __name__ == '__main__':
    main()
