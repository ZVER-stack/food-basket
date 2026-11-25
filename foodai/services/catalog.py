"""Address-scoped canonical catalog adapter owned by student 1."""
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from decimal import ROUND_CEILING
import json
from pathlib import Path
import re
from rapidfuzz.fuzz import WRatio

from foodai.models import Product, jsonable, number
from foodai.services.units import normalize_quantity
from foodai.services.matching import ingredient_kind, matches_ingredient, matches_required_term, brand_key

ZONE_IDS = ['msk_center'] + [f'msk_{i:02}' for i in range(1, 10)]


def words(text):
    return re.findall(r'[\w]+', str(text).casefold().replace('ё', 'е'))


class Catalog:
    def __init__(self, path):
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        self.demo = data.get('demo') is True
        self.coverage = data.get('coverage', [])
        self.products = {}
        self.by_zone = defaultdict(list)
        self.excluded_by_zone = defaultdict(list)
        for excluded in data.get('excluded_products', []):
            self.excluded_by_zone[excluded['zone_id']].append(excluded)
        for raw in data['products']:
            row = dict(raw)
            row['package_qty'], row['package_unit'] = normalize_quantity(row['package_qty'], row['package_unit'])
            row['price_rub'] = number(row['price_rub'])
            if row['price_rub'] <= 0 or not row.get('captured_at'):
                raise ValueError('У товара нужны положительная цена и дата выгрузки')
            datetime.fromisoformat(row['captured_at'])
            if row['zone_id'] not in ZONE_IDS:
                raise ValueError('Каталог должен использовать одну из 10 зон проекта')
            if row.get('available') is not None and type(row['available']) is not bool:
                raise ValueError('Наличие должно быть true, false или null')
            product = Product(**row)
            key = (product.storefront_id, product.zone_id, product.sku)
            if key in self.products:
                raise ValueError('Повтор SKU в одной витрине и зоне')
            self.products[key] = product
            self.by_zone[product.zone_id].append(product)
        self.manifest = {(c['zone_id'], c['storefront_id']): c for c in self.coverage
                         if c.get('status') in ('complete', 'collected_with_gaps', 'demo')}

    def storefronts(self, zone_id):
        return sorted(storefront for zone, storefront in self.manifest if zone == zone_id)

    def get_product_details(self, sku, storefront_id, zone_id):
        product = self.products.get((storefront_id, zone_id, str(sku)))
        return jsonable(asdict(product)) if product else None

    def missing_reason(self, query, zone_id, brand=None):
        if not ingredient_kind(query):
            return None
        for p in self.excluded_by_zone[zone_id]:
            if p.get('reason') != 'unverified_ordinary_price':
                continue
            if brand and brand_key(p.get('brand', '')) != brand_key(brand):
                continue
            if matches_ingredient(query, p['name'], p.get('category', '')):
                return 'unverified_ordinary_price'
        return None

    def search_products(self, query, zone_ids, filters=None, limit=5):
        filters = filters or {}
        if not isinstance(query, str) or not query.strip() or len(query) > 250:
            raise ValueError('Поисковый запрос должен содержать от 1 до 250 символов')
        if not 1 <= limit <= 15 or len(zone_ids) != 1:
            raise ValueError('Один поиск использует одну выбранную зону и 1–15 кандидатов на витрину')
        if set(filters) - {'brand', 'exclude_brands', 'required_terms', 'exclude_terms', 'storefront_id', 'unit', 'quantity', 'package_qty'}:
            raise ValueError('Неизвестный фильтр')
        requested_quantity = number(filters['quantity']) if 'quantity' in filters else None
        if requested_quantity is not None and requested_quantity <= 0:
            raise ValueError('Количество должно быть положительным')
        kind = ingredient_kind(query)
        zone_id = zone_ids[0]
        allowed = set(self.storefronts(zone_id))
        grouped = defaultdict(list)
        for product in self.by_zone[zone_id]:
            if product.storefront_id not in allowed or product.available is not True:
                continue
            text = ' '.join(words(product.name))
            if filters.get('storefront_id') and product.storefront_id != filters['storefront_id']:
                continue
            if filters.get('unit') and product.package_unit != filters['unit']:
                continue
            if filters.get('package_qty') and product.package_qty != number(filters['package_qty']):
                continue
            if not matches_ingredient(query, product.name, product.category):
                continue
            if filters.get('brand') and brand_key(product.brand) != brand_key(filters['brand']):
                continue
            if any(brand_key(product.brand) == brand_key(b) for b in filters.get('exclude_brands', [])):
                continue
            if any(not matches_required_term(t, text) for t in filters.get('required_terms', [])):
                continue
            if any(t.casefold().replace('ё', 'е') in text for t in filters.get('exclude_terms', [])):
                continue
            # Raw ingredients must not silently match prepared or smoked foods.
            if not any(t in query.lower() for t in ('копч', 'готов', 'колбас')) and any(
                    t in text for t in ('копчен', 'готовое блюдо', 'колбас')):
                continue
            score = WRatio(' '.join(words(query)), text)
            if kind:
                score = max(score, 90)
            if score >= 58:
                grouped[product.storefront_id].append((score, product))
        def rank(value):
            score, product = value
            price = product.price_rub
            if requested_quantity is not None:
                packs = (requested_quantity / product.package_qty).to_integral_value(rounding=ROUND_CEILING) if filters.get('unit') else requested_quantity
                price *= packs
            return (price, -score, product.sku) if kind and requested_quantity is not None else (-score, price, product.sku)
        return {store: [self.get_product_details(p.sku, store, zone_id)
                        for _, p in sorted(values, key=rank)[:limit]]
                for store, values in grouped.items()}
