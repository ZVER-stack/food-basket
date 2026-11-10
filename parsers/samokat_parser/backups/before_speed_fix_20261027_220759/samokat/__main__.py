from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import sys
from pathlib import Path

from .core import (COLUMNS, ROOT, REQUIRED_PRODUCT, ParserError, ResultStore,
                   SchemaError, load_zones, normalize_product, now_moscow, to_row)

LOG = logging.getLogger('samokat')


def arguments():
    parser = argparse.ArgumentParser(description='Парсер публичной витрины Самоката через Firefox')
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('zones', 'probe', 'run', 'recover'):
        p = commands.add_parser(command)
        p.add_argument('--zones', type=Path, default=ROOT / 'data/hexagons.csv')
        p.add_argument('--okrug', nargs='+', help='Например: ЦАО САО')
        p.add_argument('--h3', nargs='+', help='Точные h3_index')
        p.add_argument('--limit', type=int, help='Первые N зон после фильтров')
        if command == 'zones':
            continue
        p.add_argument('--firefox', help='Полный путь к firefox.exe при необходимости')
        p.add_argument('--geckodriver', help='Полный путь к geckodriver.exe при необходимости')
        p.add_argument('--interval', type=float, default=3.0, help='Пауза запросов API; по умолчанию 3 секунды, минимум 1')
        p.add_argument('--startup-timeout', type=int, default=240)
        p.add_argument('--headless', action='store_true', help='По умолчанию Firefox видимый')
        p.add_argument('--verbose', action='store_true')
        p.add_argument('--capture-dir', type=Path, required=command == 'recover',
                       help='Папка публичных ответов; для run по умолчанию <output>.responses')
        p.add_argument('--fresh-requests', action='store_true', help='Не использовать старые ответы; для нового среза нужен новый --output')
        if command == 'recover':
            p.set_defaults(limit=1)
        if command == 'probe':
            p.add_argument('--output-dir', type=Path, default=ROOT / 'results/probe')
        else:
            p.add_argument('--output', type=Path)
            p.add_argument('--surname', default='students')
            p.add_argument('--page-size', type=int, default=100)
            p.add_argument('--details', action='store_true', help='Загружать подробную карточку каждого товара; существенно медленнее')
            p.add_argument('--cache-ttl', type=int, default=900, help='Срок кэша склада в секундах; 0 отключает')
    p = commands.add_parser('validate')
    p.add_argument('csv_file', type=Path)
    return parser, parser.parse_args()


def selected_zones(args):
    rows = load_zones(args.zones)
    if args.okrug:
        unknown = set(args.okrug) - {r['okrug'] for r in rows}
        if unknown:
            raise ParserError('Неизвестные округа: ' + ', '.join(sorted(unknown)))
        rows = [r for r in rows if r['okrug'] in args.okrug]
    if args.h3:
        unknown = set(args.h3) - {r['h3_index'] for r in rows}
        if unknown:
            raise ParserError('Неизвестные/исключённые фильтром зоны: ' + ', '.join(sorted(unknown)))
        rows = [r for r in rows if r['h3_index'] in args.h3]
    if args.limit is not None:
        if args.limit < 1:
            raise ParserError('--limit должен быть положительным')
        rows = rows[:args.limit]
    if not rows:
        raise ParserError('Фильтры не выбрали ни одной зоны')
    return rows


def setup_logging(path, verbose):
    path.parent.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(), logging.FileHandler(path, encoding='utf-8')]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s', handlers=handlers, force=True)
    # Сетевой debug Selenium может включать служебные заголовки — отключён всегда.
    logging.getLogger('selenium').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)


def browser(args, capture=True):
    from .firefox import FirefoxAPI
    return FirefoxAPI(binary=args.firefox, geckodriver=args.geckodriver,
                      interval=args.interval, startup_timeout=args.startup_timeout, headless=args.headless,
                      capture_dir=args.capture_dir if capture else None)


def probe(args, zones):
    from .catalog import Catalog, category_paths, choose_showcase, food_categories
    args.output_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(args.output_dir / 'probe.log', args.verbose)
    zone = zones[0]
    report = {'mode': 'probe', 'complete_catalog': False, 'h3_index': zone['h3_index'],
              'address': zone['address'], 'checked_at': now_moscow()}
    with browser(args) as api:
        showcase = choose_showcase(api.query('/showcases/list', params={
            'lat': float(zone['address_lat']), 'lon': float(zone['address_lon'])}))
        report['web_version'] = api.version
        report['showcase'] = showcase
        if showcase is None:
            report['no_delivery_confirmed'] = True
        else:
            sid = showcase['showcaseId']
            categories = api.query(f'/v2/showcases/{sid}/categories/list')
            paths = category_paths(categories, all_paths=True)
            roots = food_categories(categories)
            allowed = [p for options in roots.values() for p in options]
            paths = {cid: next((p for p in options if any(p == a or p.startswith(a + ' > ') for a in allowed)), '')
                     for cid, options in paths.items()}
            paths = {cid: p for cid, p in paths.items() if p}
            report['categories_count'] = len(categories)
            # Образец одной страницы; это не полный проход и не контрольная точка.
            parent_ids = {str(c.get('parentId')) for c in categories}
            candidates = [cid for cid in paths if cid not in parent_ids] + list(paths)
            rows = []
            for cid in dict.fromkeys(candidates):
                page = next(Catalog(api).pages(sid, cid))
                rows = [to_row(normalize_product(p, paths[cid], {'store_id': showcase.get('storeId', '')}), zone)
                        for p in page if not p.get('virtualPack')]
                if rows:
                    report.update(category=paths[cid], sample_rows=len(rows))
                    break
            if not rows:
                raise SchemaError('Проба не нашла товаров')
            with (args.output_dir / 'sample_partial.csv').open('w', encoding='utf-8', newline='') as f:
                w = csv.DictWriter(f, fieldnames=COLUMNS)
                w.writeheader()
                w.writerows(rows)
    (args.output_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    LOG.info('Проба завершена. Это образец, не полный каталог: %s', args.output_dir)


def run(args, zones, offline=False):
    from .catalog import Catalog
    from .captures import QuerySession, MissingCapture
    if not 1 <= args.page_size <= 100 or args.cache_ttl < 0:
        raise ParserError('--page-size должен быть от 1 до 100, --cache-ttl неотрицательным')
    if not re.fullmatch(r'[A-Za-zА-Яа-яЁё0-9_-]+', args.surname):
        raise ParserError('--surname: используйте буквы, цифры, дефис или подчёркивание')
    output = args.output or ROOT / 'results' / f'samokat_{now_moscow()[:10]}_{args.surname}.csv'
    setup_logging(output.with_suffix('.log'), args.verbose)
    identity = {'zones_sha256': hashlib.sha256(args.zones.read_bytes()).hexdigest(),
                'columns': COLUMNS, 'details': args.details, 'scope': 'food', 'schema': 'samokat-web-v2'}
    store = ResultStore(output, identity)
    try:
        pending = [z for z in zones if not store.done(z['h3_index'])]
        LOG.info('Выбрано %s зон; уже завершено %s; осталось %s', len(zones), len(zones)-len(pending), len(pending))
        if pending:
            captures = args.capture_dir or output.with_suffix('.responses')
            with QuerySession(captures, factory=lambda: browser(args, capture=False),
                              offline=offline, reuse=not args.fresh_requests) as api:
                catalog = Catalog(api, page_size=args.page_size, details=args.details, cache_ttl=args.cache_ttl)
                for i, zone in enumerate(pending, 1):
                    LOG.info('Зона [%s/%s] %s: %s', i, len(pending), zone['h3_index'], zone['address'])
                    def progress(products, issues, zone=zone):
                        store.save_partial(zone, products, issues=issues)
                        LOG.info('Контрольная точка: %s товаров, %s карточек на разбор; %s',
                                 len(products), len(issues), output.with_suffix('.partial.csv'))
                    try:
                        products = catalog.get_catalog(zone['address'], zone['address_lat'], zone['address_lon'],
                                                       on_progress=progress)
                    except MissingCapture as error:
                        if not offline:
                            raise
                        LOG.warning('%s: %s', zone['h3_index'], error)
                        continue
                    store.save_zone(zone, products, no_delivery=not products, issues=catalog.issues)
                    LOG.info('Сохранено %s товаров, %s карточек на разбор; CSV: %s', len(products), len(catalog.issues), output)
                LOG.info('Ответов из записей: %s; новых сетевых запросов: %s', api.hits, api.network_queries)
        unfinished = [z['h3_index'] for z in zones if not store.done(z['h3_index'])]
        LOG.info('Результат: %s. Обработанные зоны: %s. Не завершены: %s', output, store.summary(), unfinished)
    finally:
        store.close()


def validate(path):
    known = {z['h3_index'] for z in load_zones(ROOT / 'data/hexagons.csv')}
    seen = set()
    empty_zones, product_zones = set(), set()
    count = 0
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != COLUMNS:
            raise SchemaError('Заголовок CSV не совпадает с 37 колонками template.csv')
        for line, row in enumerate(reader, 2):
            count += 1
            if None in row or any(v is None for v in row.values()):
                raise SchemaError(f'Строка {line}: неверное число полей')
            if row['service'] != 'samokat' or row['h3_index'] not in known:
                raise SchemaError(f'Строка {line}: неверный service/h3_index')
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', row['parsed_at']):
                raise SchemaError(f'Строка {line}: неверный parsed_at')
            key = (row['h3_index'], row['product_id'])
            if key in seen:
                raise SchemaError(f'Строка {line}: повтор zone/product_id')
            seen.add(key)
            if not row['product_id']:
                if any(row[k] for k in COLUMNS if k not in ('service','h3_index','parsed_at')):
                    raise SchemaError(f'Строка {line}: неправильная строка отсутствия доставки')
                empty_zones.add(row['h3_index'])
            else:
                product_zones.add(row['h3_index'])
                for key in REQUIRED_PRODUCT:
                    if row[key] == '':
                        raise SchemaError(f'Строка {line}: пустое обязательное поле {key}')
                for key in ('price', 'price_regular'):
                    if not re.fullmatch(r'\d+(\.\d+)?', row[key]):
                        raise SchemaError(f'Строка {line}: неверный формат цены {key}')
                if row['extra']:
                    json.loads(row['extra'])
    if empty_zones & product_zones:
        raise SchemaError('В зоне одновременно есть товары и строка отсутствия доставки')
    print(f'CSV корректен: {count} строк, {len(product_zones)} зон с товарами, {len(empty_zones)} без доставки. Полнота каталога по CSV не проверяется.')


def main():
    parser, args = arguments()
    try:
        if args.command == 'validate':
            validate(args.csv_file)
        else:
            zones = selected_zones(args)
            if args.command == 'zones':
                for z in zones:
                    print(f"{z['h3_index']} | {z['okrug']} | {z['address']}")
                print(f'Зон: {len(zones)}')
            else:
                if args.interval < 1 or args.startup_timeout < 30:
                    raise ParserError('--interval >= 1; --startup-timeout >= 30')
                if args.command == 'probe':
                    probe(args, zones)
                else:
                    run(args, zones, offline=args.command == 'recover')
    except KeyboardInterrupt:
        print('\nСбор прерван. Завершённые зоны сохранены; повторите ту же команду для продолжения.', file=sys.stderr)
        return 130
    except Exception as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        print('Проход не завершён. Готовые зоны — в CSV; промежуточные данные — в .partial.csv/SQLite и папке ответов. Повторите ту же команду для продолжения.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
