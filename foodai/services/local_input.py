"""Small offline fallback with explicit quantity clarification, not an LLM."""
import re
from foodai.services.units import normalize_quantity

UNITS = r'(?:килограмм(?:а|ов)?|кило|кг|грамм(?:а|ов)?|г|миллилитр(?:а|ов)?|мл|литр(?:а|ов)?|л|пачк(?:а|и|у|ок)|упаков(?:ка|ки|ку|ок)|штук(?:а|и)?|шт\.?)'
NUMBERS = {'один': '1', 'одна': '1', 'одну': '1', 'два': '2', 'две': '2', 'три': '3', 'полтора': '1.5'}
QUANTITY = rf'(?<!\w)(?:(?P<qty>\d+(?:[.,]\d+)?|{"|".join(NUMBERS)})\s*)?(?P<unit>{UNITS})(?!\w)'
BASIC_ALIASES = {'молоко': 'молоко', 'молока': 'молоко', 'яйца': 'яйца', 'яиц': 'яйца',
                 'гречка': 'гречка', 'гречки': 'гречка', 'гречку': 'гречка', 'рис': 'рис', 'риса': 'рис',
                 'хлеб': 'хлеб', 'хлеба': 'хлеб', 'салат': 'салат', 'салата': 'салат',
                 'капуста': 'капуста', 'капусты': 'капуста', 'чипсы': 'чипсы', 'чипсов': 'чипсы',
                 'кола': 'кола', 'колу': 'кола', 'колы': 'кола',
                 'кола без сахара': 'кола без сахара', 'колу без сахара': 'кола без сахара'}


def basic_query(query):
    return BASIC_ALIASES.get(query.casefold().replace('ё', 'е').strip())


def parsed_quantity(match):
    qty = match['qty'] or '1'
    qty = NUMBERS.get(qty, qty)
    unit = match['unit']
    if unit.startswith(('кило', 'кг')):
        unit = 'кг'
    elif unit.startswith(('грамм', 'г')):
        unit = 'г'
    elif unit.startswith(('миллилитр', 'мл')):
        unit = 'мл'
    elif unit.startswith(('литр', 'л')):
        unit = 'л'
    elif unit.startswith(('пач', 'упаков')):
        unit = 'pack'
    else:
        unit = 'шт'
    value, unit = normalize_quantity(qty, unit)
    return {'quantity': str(value), 'unit': unit}


def prepare_items(text, pending=None):
    text = text.casefold().replace('ё', 'е').strip().rstrip('!?')
    items = [dict(item) for item in pending or []]
    quantity_only = re.fullmatch(QUANTITY, text)
    if items and re.fullmatch(r'\d+(?:[.,]\d+)?', text):
        return [], 'Уточните единицу: литры, граммы или штуки. Например: «' + text + ' л». Список сохранён.', items
    if items and quantity_only:
        next_item = next(item for item in items if 'quantity' not in item)
        next_item.update(parsed_quantity(quantity_only))
    else:
        text = re.sub(r'^(?:(?:пожалуйста|мне|нужно|нужны|надо|хочу|купи|купить|найди|найти)\s+)+', '', text)
        text = re.sub(r'\s*,?\s*пожалуйста$', '', text)
        items = []
        for index, part in enumerate(re.split(r'[,;\n](?!\d)|\s+и\s+', text)):
            part = part.strip()
            if not part:
                continue
            match = re.search(QUANTITY, part)
            query = (part[:match.start()] + ' ' + part[match.end():]).strip(' .:-') if match else part
            if not query or not re.search('[а-яa-z]', query):
                return [], 'Напишите, что хотите купить. Например: «Купи молоко и яйца».', []
            item = {'id': f'item_{index}', 'query': query, 'required_terms': [], 'exclude_terms': [], 'exclude_brands': []}
            if match:
                item.update(parsed_quantity(match))
            items.append(item)
    if not items or len(items) > 15:
        return [], 'Напишите список от 1 до 15 продуктов.', []
    missing = next((item for item in items if 'quantity' not in item), None)
    if missing:
        names = ', '.join(item['query'] for item in items)
        return [], (f'Запомнил список: {names}.\nУкажите количество для позиции «{missing["query"]}». '
                    'Можно ответить «1 л», «500 г» или «10 шт.». Остальные позиции сохранены.'), items
    return items, '', []
