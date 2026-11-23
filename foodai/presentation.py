"""Human-readable labels; identifiers and exact values stay in structured results."""
from datetime import datetime
from foodai.models import number


def decimal_text(value):
    return format(number(value).normalize(), 'f').replace('.', ',')


def money(value):
    return decimal_text(value) + ' ₽'


def package_size(quantity, unit):
    qty = number(quantity)
    if unit == 'ml' and qty >= 1000:
        qty, unit = qty / 1000, 'л'
    elif unit == 'g' and qty >= 1000:
        qty, unit = qty / 1000, 'кг'
    return f"{decimal_text(qty)} { {'ml': 'мл', 'g': 'г', 'pcs': 'шт.'}.get(unit, unit)}"


def store_name(storefront_id, service=''):
    key = storefront_id.split(':')[0]
    names = {'ozon_fresh': 'Ozon Fresh', 'samokat': 'Самокат', 'vkusvill': 'ВкусВилл',
             'lavka_small': 'Яндекс Лавка', 'lavka_big': 'Большая Лавка',
             'yandex_lavka': 'Яндекс Лавка', 'lavka': 'Яндекс Лавка'}
    return names.get(key) or names.get(service) or 'Магазин'


def price_dates(baskets):
    dates = sorted({datetime.fromisoformat(row['captured_at']).date()
                    for basket in baskets for row in basket['lines']})
    if not dates:
        return ''
    label = dates[0].strftime('%d.%m.%Y')
    if len(dates) > 1:
        label += '–' + dates[-1].strftime('%d.%m.%Y')
    return 'Цены по данным на ' + label + '.'
