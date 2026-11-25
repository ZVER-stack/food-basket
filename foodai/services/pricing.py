"""Minimal shared calculator for basket-agent integration, replaceable by student 4."""
from collections import defaultdict
from decimal import ROUND_CEILING
from foodai.models import number, jsonable
from foodai.services.matching import matches_ingredient


def calculate_basket(requirements, selections, storefront_id, catalog, zone_id):
    needs = {r['id']: r for r in requirements}
    if len(needs) != len(requirements):
        raise ValueError('Повтор идентификатора позиции')
    selected = {}
    for choice in selections:
        key = choice['requirement_id']
        if key not in needs or key in selected:
            raise ValueError('Неизвестная или повторная позиция')
        selected[key] = str(choice['sku'])
    missing, grouped = [], defaultdict(list)
    for key, req in needs.items():
        product = catalog.get_product_details(selected.get(key), storefront_id, zone_id)
        if (not product or product['available'] is not True
                or not matches_ingredient(req['query'], product['name'], product.get('category', ''))
                or (req['unit'] != 'pack' and product['package_unit'] != req['unit'])):
            missing.append(req['query'])
            continue
        grouped[product['sku']].append((req, product))
    lines, total = [], number(0)
    for sku, values in grouped.items():
        product = values[0][1]
        packs_requested = sum((number(req['quantity']) for req, _ in values if req['unit'] == 'pack'), number(0))
        quantity = sum((number(req['quantity']) for req, _ in values if req['unit'] != 'pack'), number(0))
        packs = int(packs_requested + (quantity / number(product['package_qty'])).to_integral_value(rounding=ROUND_CEILING))
        subtotal = packs * number(product['price_rub'])
        total += subtotal
        lines.append({'queries': [r['query'] for r, _ in values], 'sku': sku,
                      'name': product['name'], 'package_qty': product['package_qty'],
                      'package_unit': product['package_unit'], 'packages': packs,
                      'package_price': product['price_rub'], 'subtotal': subtotal,
                      'captured_at': product['captured_at'], 'product_url': product['product_url']})
    manifest = catalog.manifest.get((zone_id, storefront_id), {})
    return jsonable({'storefront_id': storefront_id, 'service': manifest.get('service', ''),
                     'complete': bool(needs) and not missing, 'total': total, 'lines': lines,
                     'missing': missing, 'catalog_status': manifest.get('status', 'unknown')})


def compare_stores(baskets):
    full = sorted((b for b in baskets if b['complete']), key=lambda b: number(b['total']))
    return {'full': full, 'partial': [b for b in baskets if not b['complete']],
            'winner': full[0]['storefront_id'] if full else None}
