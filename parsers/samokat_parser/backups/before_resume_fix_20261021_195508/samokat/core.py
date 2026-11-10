"""Формат проекта, проверка данных и транзакционное сохранение."""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COLUMNS = next(csv.reader((ROOT / 'data/template.csv').read_text(encoding='utf-8-sig').splitlines()))
MSK = timezone(timedelta(hours=3), 'Europe/Moscow')
REQUIRED_PRODUCT = ('product_id', 'name', 'price', 'price_regular', 'amount')


class ParserError(RuntimeError):
    pass


class Blocked(ParserError):
    """Блокировка не означает отсутствие доставки."""


class SchemaError(ParserError):
    pass


def now_moscow():
    return datetime.now(MSK).strftime('%Y-%m-%d %H:%M:%S')


def number(value):
    if value is None or value == '':
        return ''
    if isinstance(value, bool):
        raise SchemaError('Логическое значение вместо числа')
    text = str(value).replace('\xa0', '').replace(' ', '').replace(',', '.')
    try:
        result = Decimal(text)
    except InvalidOperation as e:
        raise SchemaError(f'Некорректное число: {value!r}') from e
    if not result.is_finite():
        raise SchemaError(f'Неконечное число: {value!r}')
    return format(result, 'f').rstrip('0').rstrip('.') if '.' in format(result, 'f') else str(result)


def rubles(value):
    return '' if value is None or value == '' else number(Decimal(number(value)) / 100)


def text(value):
    if value is None:
        return ''
    if not isinstance(value, (str, int, float)):
        return ''
    return html.unescape(re.sub(r'<[^>]+>', '', re.sub(r'<br\s*/?>', '\n', str(value), flags=re.I))).strip()


def amount_from_product(raw):
    if text(raw.get('specification')):
        return text(raw['specification']), 'specification'
    # Текущая витрина часто включает фасовку только в название. Извлекаем
    # буквальную запись в конце, не вычисляя массу из состава/калорий/цены.
    name = text(raw.get('name'))
    numeric = r'\d+(?:[.,]\d+)?'
    unit = r'(?:мг|кг|г|мл|л|шт\.?|штук[аи]?|уп\.?|упаковк[аи]|пакет(?:а|ов)?|капсул[аы]?|таблет(?:ка|ки|ок)|пар[аы]?)'
    single = rf'{numeric}\s*{unit}'
    pattern = rf'(?<![\w.])((?:(?:{single}|{numeric})\s*(?:[×xх*]|,)\s*)?{single})\s*$'
    match = re.search(pattern, name, flags=re.I)
    return (match.group(1), 'name') if match else ('', '')


def load_zones(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    required = {'h3_index', 'address', 'address_lat', 'address_lon', 'okrug'}
    if not rows or not required.issubset(rows[0]):
        raise SchemaError(f'В {path} нет обязательных колонок зон')
    seen = set()
    for row in rows:
        key = row['h3_index']
        if not re.fullmatch(r'87[0-9a-f]{13}', key) or key in seen:
            raise SchemaError(f'Некорректный или повторный h3_index: {key}')
        seen.add(key)
        for column, lo, hi in [('address_lat', -90, 90), ('address_lon', -180, 180)]:
            value = Decimal(number(row[column]))
            if not lo <= value <= hi:
                raise SchemaError(f'{key}: координаты вне диапазона')
        if not row['address'].strip():
            raise SchemaError(f'{key}: нет адреса')
    return rows


def normalize_product(raw, category, metadata):
    """Схема публичного web-клиента Самоката 2.86.1; цены API в копейках."""
    if not isinstance(raw, dict) or not isinstance(raw.get('prices'), dict):
        raise SchemaError('Товар не соответствует схеме uuid/name/prices/specification')
    prices = raw['prices']
    attrs = raw.get('attributes') or {}
    if not isinstance(attrs, dict):
        raise SchemaError('attributes товара должен быть объектом')
    details = {}
    for item in raw.get('detailsAttributes') or []:
        value = item.get('value')
        if value is not None:
            details[text(item.get('displayName')).lower()] = f"{value} {item.get('unit') or ''}".strip()

    def attribute(*names):
        for name in names:
            value = attrs.get(name)
            if value is not None and value != '':
                return text(value)
            value = details.get(name.lower())
            if value is not None and value != '':
                return text(value)
        return ''

    current = rubles(prices.get('current'))
    old = rubles(prices.get('old'))
    if current == '' or Decimal(current) < 0:
        raise SchemaError(f"{raw.get('uuid')}: нет допустимой текущей цены")
    regular = old or current
    if Decimal(regular) < Decimal(current):
        raise SchemaError(f"{raw.get('uuid')}: обычная цена ниже текущей")
    media = raw.get('media') or []
    image = next((m.get('url', '') for m in media if m.get('type') == 'IMAGE'), '')
    labels = []
    for badge in [raw.get('badge'), (raw.get('promoMarkup') or {}).get('badge')]:
        if isinstance(badge, dict) and badge.get('text'):
            labels.append(text(badge['text']))
    for highlight in raw.get('highlights') or []:
        label = text(highlight.get('text')) if isinstance(highlight, dict) else text(highlight)
        if label:
            labels.append(label)
    qty = raw.get('quantity')
    amount, amount_source = amount_from_product(raw)
    row = {
        **metadata,
        'parsed_at': now_moscow(),
        'product_id': text(raw.get('uuid')),
        'name': text(raw.get('name')),
        'amount': amount,
        'category': category,
        'price': current,
        'price_regular': regular,
        # amazing/promoXY зависят от условий акции: не выдаём их за цену по карте.
        'promo': text((raw.get('promoMarkup') or {}).get('promoDescription')),
        'brand': attribute('brand', 'Бренд'),
        'manufacturer': attribute('manufacturer', 'Производитель', 'Изготовитель'),
        'country': attribute('country', 'Страна производства', 'Страна-производитель', 'Страна'),
        'composition': attribute('composition', 'Состав'),
        'shelf_life': attribute('shelfLife', 'Срок годности'),
        'storage_conditions': attribute('storageConditions', 'Условия хранения'),
        'description': text(raw.get('description')),
        'labels': '; '.join(dict.fromkeys(labels)),
        'image_url': image,
        'url': 'https://samokat.ru/product/' + (raw.get('slug') or text(raw.get('uuid'))),
        'in_stock': '' if qty is None else ('true' if Decimal(number(qty)) > 0 else 'false'),
    }
    # Остаток API может ограничивать заказ, это не доказанный физический stock_qty.
    for column, key in [('kcal', 'kilocalories'), ('protein', 'protein'), ('fat', 'fats'), ('carbs', 'carbohydrates')]:
        value = attrs.get(key)
        if value is not None and value != '':
            row[column] = number(value)
    if prices.get('discountPercent') is not None:
        row['discount_percent'] = number(prices['discountPercent'])
    row['extra'] = {'source': 'samokat_web_api', 'categories': [category], 'raw_product': raw,
                    'amount_source': amount_source}
    for key in REQUIRED_PRODUCT:
        if row.get(key) is None or row.get(key) == '':
            raise SchemaError(f"{raw.get('uuid')}: отсутствует обязательное поле {key}")
    return row


def to_row(product, zone, timestamp=None):
    row = {k: v for k, v in product.items() if k in COLUMNS}
    extra = row.pop('extra', {}) or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = {'original_extra': extra}
    if not isinstance(extra, dict):
        extra = {'original_extra': extra}
    extra = {**extra, **{k: v for k, v in product.items() if k not in COLUMNS}}
    if product:
        extra['zone_metadata'] = {k: zone[k] for k in ('okrug', 'district', 'address_in_zone', 'address_dist_m') if k in zone}
    if extra:
        row['extra'] = json.dumps(extra, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    row.update(service='samokat', h3_index=zone['h3_index'], parsed_at=timestamp or product.get('parsed_at') or now_moscow())
    return {column: ('' if row.get(column) is None else row.get(column, '')) for column in COLUMNS}


class ResultStore:
    """SQLite — контрольная точка; CSV дописывается, при запуске восстанавливается."""
    def __init__(self, csv_path, identity):
        self.path = Path(csv_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db_path = self.path.with_suffix('.sqlite3')
        if self.path.exists() and not db_path.exists():
            raise ParserError('CSV существует без контрольной базы. Выберите другой --output.')
        self.db = sqlite3.connect(db_path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS zones (
                h3 TEXT PRIMARY KEY, status TEXT NOT NULL, count INTEGER NOT NULL, finished TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS products (
                h3 TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(h3,id));
        ''')
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        saved = self.db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
        if saved and saved[0] != fingerprint:
            self.db.close()
            raise ParserError('Контрольная база относится к другим входным данным/настройкам. Выберите новый --output.')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('identity',?)", (fingerprint,))
        self._last_rowid = 0
        self._csv_offset = 0
        self.export(rebuild=True)

    def done(self, h3):
        return self.db.execute('SELECT 1 FROM zones WHERE h3=?', (h3,)).fetchone() is not None

    def save_zone(self, zone, products, no_delivery=False):
        if self.done(zone['h3_index']):
            return self.export()
        if no_delivery and products:
            raise ParserError('no_delivery несовместим с товарами')
        if not no_delivery and not products:
            raise SchemaError('Пустой каталог при доступной доставке — ошибка, зона не завершена')
        rows = [to_row(p, zone) for p in products] if products else [to_row({}, zone)]
        identifiers = [r['product_id'] for r in rows]
        if len(set(identifiers)) != len(identifiers):
            raise SchemaError('Повтор product_id в зоне')
        with self.db:
            self.db.execute('DELETE FROM products WHERE h3=?', (zone['h3_index'],))
            self.db.executemany('INSERT INTO products VALUES (?,?,?)', [
                (zone['h3_index'], r['product_id'], json.dumps(r, ensure_ascii=False, allow_nan=False)) for r in rows])
            self.db.execute('INSERT OR REPLACE INTO zones VALUES (?,?,?,?)', (
                zone['h3_index'], 'no_delivery' if no_delivery else 'ok', len(products), now_moscow()))
        self.export()

    def export(self, rebuild=False):
        if rebuild:
            temp = self.path.with_suffix('.csv.tmp')
            last = 0
            with temp.open('w', encoding='utf-8', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=COLUMNS)
                writer.writeheader()
                for last, data in self.db.execute('SELECT rowid, data FROM products ORDER BY rowid'):
                    writer.writerow(json.loads(data))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path)
            self._last_rowid = last
            self._csv_offset = self.path.stat().st_size
            return
        last = self._last_rowid
        # Дописываем только новую зону: полный CSV не переписывается 201 раз.
        # В случае сбоя SQLite остаётся источником истины; следующий запуск
        # атомарно пересоберёт CSV, включая оборванную при записи зону.
        with self.path.open('r+b') as f:
            f.seek(self._csv_offset)
            f.truncate()
            buffer = io.StringIO(newline='')
            writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
            for last, data in self.db.execute('SELECT rowid, data FROM products WHERE rowid>? ORDER BY rowid', (last,)):
                writer.writerow(json.loads(data))
                if buffer.tell() >= 1024 * 1024:
                    f.write(buffer.getvalue().encode('utf-8'))
                    buffer.seek(0)
                    buffer.truncate(0)
            f.write(buffer.getvalue().encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())
            self._csv_offset = f.tell()
        self._last_rowid = last

    def summary(self):
        return dict(self.db.execute('SELECT status, COUNT(*) FROM zones GROUP BY status'))

    def close(self):
        self.db.close()
