"""Покрытие адресов, категории, пагинация и повторное использование склада."""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import quote

from .core import SchemaError, normalize_product, now_moscow, number, rubles

LOG = logging.getLogger(__name__)
FOOD_SECTIONS = {
    'Готовая еда', 'Овощи и фрукты', 'Молоко, яйца и сыр', 'Хлеб и выпечка',
    'Мясо и рыба', 'Морозилка', 'Вода и напитки', 'Сладкое', 'Снеки',
    'Бакалея', 'ЗОЖ и веганство',
}
NON_FOOD_CATEGORIES = {
    'Фольга', 'Пакеты для запекания', 'Бумага для выпечки', 'Пергамент для выпечки',
    'Формы для выпечки', 'Столовые приборы', 'Одноразовая посуда', 'Салфетки',
    'Свечи для торта', 'Зубочистки', 'Уголь и розжиг', 'Решётки и шампуры',
}


def is_non_food_product(product):
    return bool(re.match(r'^(?:алюминиевая\s+)?фольга\b|^рукав\s+для\s+запекания\b',
                         str(product.get('name', '')).strip(), flags=re.I))


def food_categories(categories):
    """Основные пищевые разделы и детское питание; промо не задаёт охват."""
    paths = category_paths(categories, all_paths=True)
    by_id = {str(c['id']): c for c in categories}
    selected = {}
    for c in categories:
        cid = str(c['id'])
        parent = by_id.get(str(c.get('parentId')))
        if (parent is None and c['name'] in FOOD_SECTIONS) or (
                c['name'] == 'Детское питание' and parent and parent['name'] == 'Для детей'):
            selected[cid] = paths[cid]
    if not selected:
        raise SchemaError('В списке не найдены пищевые разделы; проверьте FOOD_SECTIONS после изменения сайта')
    return selected


def choose_showcase(response):
    if not isinstance(response, list):
        raise SchemaError('/showcases/list: ожидался список; отсутствие доставки не подтверждено')
    if not response:
        return None
    if not all(isinstance(item, dict) and item.get('showcaseId') for item in response):
        raise SchemaError('/showcases/list: изменилась схема showcaseId')
    for kind in ('MINIMARKET', 'SUPERMARKET'):
        for item in response:
            if item.get('type') == kind:
                return item
    return response[0]


def category_paths(categories, all_paths=False):
    if not isinstance(categories, list) or not categories:
        raise SchemaError('Доставка есть, но categories/list пуст или имеет неизвестный формат')
    nodes = {}
    for category in categories:
        if not isinstance(category, dict) or not category.get('id') or not category.get('name'):
            raise SchemaError('categories/list: ожидались id, name, parentId')
        cid = str(category['id'])
        nodes.setdefault(cid, [])
        if category not in nodes[cid]:
            nodes[cid].append(category)

    def parents(cid, seen):
        if cid in seen:
            raise SchemaError(f'Цикл родителей категории {cid}')
        result = []
        for node in nodes[cid]:
            parent = str(node.get('parentId') or '')
            prefixes = parents(parent, seen | {cid}) if parent in nodes else ['']
            for prefix in prefixes:
                path = (prefix + ' > ' if prefix else '') + str(node['name'])
                if path not in result:
                    result.append(path)
            if len(result) > 1000:
                raise SchemaError(f'Слишком много путей категории {cid}')
        return result

    paths = {cid: parents(cid, set()) for cid in nodes}
    if all_paths:
        return paths
    return {cid: max(options, key=lambda p: (p.count(' > '), len(p))) for cid, options in paths.items()}


def eta_minutes(value):
    if isinstance(value, dict):
        amount = value.get('value')
        unit = value.get('timeUnit')
        if amount is None or unit not in ('MINUTES', 'HOURS'):
            return ''
        return number(float(number(amount)) * (60 if unit == 'HOURS' else 1))
    return ''  # Текстовые обещания времени не превращаем в число догадкой.


class Catalog:
    def __init__(self, api, *, page_size=100, details=False, cache_ttl=900, max_pages=1000):
        self.api = api
        self.page_size = page_size
        self.details = details
        self.cache_ttl = cache_ttl
        self.max_pages = max_pages
        self.cache = {}

    def pages(self, showcase_id, category_id, offset=0):
        url = f'/v2/showcases/{quote(str(showcase_id), safe="")}/categories/{quote(str(category_id), safe="")}/products'
        signatures = set()
        for _ in range(self.max_pages):
            response = self.api.query(url, 'POST', {'filters': {}, 'limit': self.page_size, 'offset': offset})
            if not isinstance(response, dict) or not isinstance(response.get('items'), list):
                raise SchemaError(f'{url}: ожидались items и pageInfo.hasNextPage')
            items = response['items']
            page_info = response.get('pageInfo')
            if not isinstance(page_info, dict) or type(page_info.get('hasNextPage')) is not bool:
                raise SchemaError(f'{url}: нет явного признака завершения пагинации')
            if any(not isinstance(p, dict) or not p.get('uuid') for p in items):
                raise SchemaError(f'{url}: товар без uuid')
            signature = tuple(str(p['uuid']) for p in items)
            if signature and signature in signatures:
                raise SchemaError(f'{url}: API повторил ту же страницу; неполный каталог не сохранён')
            signatures.add(signature)
            if not items and page_info['hasNextPage']:
                raise SchemaError(f'{url}: пустая страница с hasNextPage=true')
            yield items
            if not page_info['hasNextPage']:
                return
            # Offset считается по исходным строкам, а не по уникальным товарам.
            offset += len(items)
        raise SchemaError(f'{url}: достигнут предел {self.max_pages} страниц')

    def products(self, showcase_id):
        sid = quote(str(showcase_id), safe='')
        categories = self.api.query(f'/v2/showcases/{sid}/categories/list')
        paths = food_categories(categories)
        counts = {str(c['id']): c.get('productsCount') for c in categories}
        found = {}

        def consume(items, path_options):
            for raw in items:
                if not isinstance(raw, dict) or not raw.get('uuid'):
                    raise SchemaError('Товар категории без uuid')
                if raw.get('virtualPack'):
                    continue
                if is_non_food_product(raw):
                    continue
                product_id = str(raw['uuid'])
                if product_id not in found:
                    found[product_id] = {'raw': raw, 'paths': list(path_options), 'observed_at': now_moscow()}
                else:
                    old = found[product_id]
                    for option in path_options:
                        if option not in old['paths']:
                            old['paths'].append(option)
                    if raw.get('prices') != old['raw'].get('prices'):
                        old['price_changed_during_scan'] = True
                        old['raw'] = raw
                        old['observed_at'] = now_moscow()

        def subtree(node, path_options, ancestors, loaded=False):
            if not isinstance(node, dict) or not node.get('uuid'):
                raise SchemaError('Неизвестная схема дерева категории')
            cid = str(node['uuid'])
            if str(node.get('name', '')).strip() in NON_FOOD_CATEGORIES:
                return
            if cid in ancestors:
                raise SchemaError('Цикл в загруженном дереве категорий')
            # Карточки навигации имеют pageInfo=false, но ещё не содержат
            # ассортимент. Такой узел нужно раскрыть отдельным запросом.
            if not loaded and not node.get('products') and not node.get('categories'):
                if counts.get(cid) == 0:
                    return
                node = self.api.query(f'/v2/showcases/{sid}/categories/{quote(cid, safe="")}', 'POST',
                                      {'filters': {}, 'withPagination': False, 'limit': self.page_size})
                if not isinstance(node, dict) or str(node.get('uuid')) != cid:
                    raise SchemaError('Ответ подкатегории не совпадает с запрошенным id')
            items = node.get('products', [])
            children = node.get('categories', [])
            if not isinstance(items, list) or not isinstance(children, list):
                raise SchemaError('products/categories должны быть списками')
            info = node.get('pageInfo')
            if not isinstance(info, dict) or type(info.get('hasNextPage')) is not bool:
                raise SchemaError(f'{cid}: нет явного признака завершения категории')
            consume(items, path_options)
            if info['hasNextPage']:
                for page in self.pages(showcase_id, cid, offset=len(items)):
                    consume(page, path_options)
            for child in children:
                name = child.get('name') if isinstance(child, dict) else None
                if not name:
                    raise SchemaError('Подкатегория без имени')
                child_paths = [f'{p} > {name}' for p in path_options]
                subtree(child, child_paths, ancestors | {cid})

        for i, (category_id, path_options) in enumerate(paths.items(), 1):
            path = max(path_options, key=lambda p: (p.count(' > '), len(p)))
            LOG.info('Категория [%s/%s] %s', i, len(paths), path)
            node = self.api.query(f'/v2/showcases/{sid}/categories/{quote(category_id, safe="")}', 'POST',
                                  {'filters': {}, 'withPagination': False, 'limit': self.page_size})
            if not isinstance(node, dict) or str(node.get('uuid')) != category_id:
                raise SchemaError('Ответ категории не совпадает с запрошенным id')
            subtree(node, path_options, set(), loaded=True)
        if not found:
            raise SchemaError('Все категории загружены, но товаров нет; зона не завершена')
        if self.details:
            for i, (product_id, product) in enumerate(found.items(), 1):
                LOG.info('Карточка [%s/%s] %s', i, len(found), product_id)
                detail = self.api.query(f'/v2/showcases/{sid}/products/{quote(product_id, safe="")}')
                if not isinstance(detail, dict) or str(detail.get('uuid')) != product_id:
                    raise SchemaError(f'Карточка {product_id}: отсутствует совпадающий uuid')
                product['raw'] = {**product['raw'], **detail}
                product['observed_at'] = now_moscow()
        return found

    def get_catalog(self, address, lat, lon):
        """[] только при явном пустом /showcases/list; ошибки выбрасываются."""
        coordinates = {'lat': float(lat), 'lon': float(lon)}
        showcase = choose_showcase(self.api.query('/showcases/list', params=coordinates))
        if showcase is None:
            return []
        sid = str(showcase['showcaseId'])
        store_id = str(showcase.get('storeId') or '')
        settings = self.api.query(f'/showcases/{quote(sid, safe="")}/settings', params=coordinates)
        if not isinstance(settings, dict) or str(settings.get('showcaseId')) != sid:
            raise SchemaError('settings.showcaseId не совпадает с выбранной витриной')
        # Временное закрытие склада не подменяется отсутствием покрытия.
        delivery = self.api.query('/delivery/info', params={k: f'{v:.4f}' for k, v in coordinates.items()})
        if not isinstance(delivery, dict):
            raise SchemaError('/delivery/info: ожидался объект')
        order = settings.get('orderSettings') or {}
        metadata = {
            'store_id': store_id,
            'min_order': rubles(order.get('minimalAmount')),
            'delivery_eta_min': eta_minutes(delivery.get('sla')),
        }
        # Для стоимости доставки нет подтверждённого отдельного поля в текущем
        # web-контракте; оставляем пусто. Сырые условия остаются в extra.
        key = (store_id, sid, self.details) if store_id else None
        cached = self.cache.get(key) if key else None
        reused = bool(cached and time.monotonic() - cached[0] <= self.cache_ttl)
        if reused:
            raw_products = cached[1]
            LOG.info('Переиспользован каталог склада %s, витрина %s', store_id, sid)
        else:
            started = time.monotonic()
            raw_products = self.products(sid)
            if key and self.cache_ttl > 0:
                self.cache[key] = (started, raw_products)
                if len(self.cache) > 16:
                    oldest = min(self.cache, key=lambda k: self.cache[k][0])
                    del self.cache[oldest]
        checked_at = now_moscow()
        products = []
        for entry in raw_products.values():
            raw = entry['raw']
            paths = entry['paths']
            # Предпочтение более подробному пути; остальные не теряются.
            path = max(paths, key=lambda p: (p.count(' > '), len(p)))
            row = normalize_product(raw, path, metadata)
            row['parsed_at'] = entry['observed_at']
            row['extra'].update({
                'categories': paths, 'showcase_id': sid, 'showcase_type': showcase.get('type'),
                'delivery_address': address, 'delivery_coordinates': coordinates,
                'zone_checked_at': checked_at, 'catalog_reused': reused,
                'raw_showcase': showcase, 'raw_settings': settings, 'raw_delivery': delivery,
                'price_changed_during_scan': entry.get('price_changed_during_scan', False),
            })
            products.append(row)
        return products
