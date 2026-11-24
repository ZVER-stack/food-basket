import re
from foodai.models import number

UNITS = {'г': ('g', 1), 'кг': ('g', 1000), 'мл': ('ml', 1), 'л': ('ml', 1000),
         'шт': ('pcs', 1), 'g': ('g', 1), 'kg': ('g', 1000), 'ml': ('ml', 1),
         'l': ('ml', 1000), 'pcs': ('pcs', 1), 'pack': ('pack', 1)}


def normalize_quantity(quantity, unit):
    if unit not in UNITS:
        raise ValueError('Единица должна быть g, ml или pcs (либо кг, л, шт)')
    normalized, multiplier = UNITS[unit]
    qty = number(quantity) * multiplier
    if qty <= 0 or qty > 10000000:
        raise ValueError('Нужно положительное количество до 10 миллионов единиц')
    if normalized in ('pcs', 'pack') and qty != qty.to_integral_value():
        raise ValueError('Штучные товары требуют целого количества')
    return qty, normalized


def parse_package(text):
    text = str(text).lower().strip().rstrip('.').replace('ё', 'е')
    match = re.fullmatch(r'(\d+(?:[.,]\d+)?)(?:\s*шт)?\s*[×xх]\s*(\d+(?:[.,]\d+)?)\s*(кг|г|мл|л|шт)', text)
    if match:
        return normalize_quantity(number(match[1]) * number(match[2]), match[3])
    # Preserve decimal points as well as decimal commas.
    match = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*(кг|г|мл|л|шт)', str(text))
    if not match:
        raise ValueError('Фасовка не определена однозначно')
    return normalize_quantity(match[1], match[2])
