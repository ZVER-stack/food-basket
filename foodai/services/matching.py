"""Conservative identity checks for basic ingredients, shared by search and pricing."""
import re


def normalized(text):
    return str(text).casefold().replace('ё', 'е')


def brand_key(text):
    key = re.sub(r'[^\w]', '', normalized(text))
    return 'lays' if key in ('lays', 'лейс', 'лэйс') else key


def sugar_free(text):
    return bool(re.search(r'\b(?:без\s+сахара|zero|зеро|sugar[- ]?free|0\s*сахара)\b', normalized(text)))


def matches_required_term(term, name):
    return sugar_free(name) if sugar_free(term) else normalized(term) in normalized(name)


def can_select_without_model(query):
    # Extra varieties, brands or dietary restrictions require semantic selection.
    return normalized(query).strip() in {
        'гречка', 'гречки', 'крупа гречневая', 'гречневая крупа', 'гречневая ядрица',
        'рис', 'риса', 'салат', 'листовой салат', 'салат листовой', 'айсберг', 'романо', 'салат айсберг', 'салат романо',
        'молоко', 'молока', 'молоко безлактозное', 'чипсы', 'чипсов',
        'картофельные чипсы', 'чипсы картофельные', 'кола', 'кола без сахара', 'кола zero', 'кола зеро'}


def ingredient_kind(query):
    query = normalized(query)
    if re.search(r'\bкол[ауы]\b|\b(?:coca[- ]?cola|coolcola|cola)\b', query):
        return 'cola'
    if re.fullmatch(r'чипс(?:ы|ов)|(?:картофельные чипсы|чипсы картофельные)', query):
        return 'potato_chips'
    if re.fullmatch(r'молок[оа](?: безлактозное)?', query):
        return 'milk'
    if re.fullmatch(r'(?:салат|салата|(?:листовой|зеленый) салат|салат (?:листовой|зеленый)|листья салата|(?:салат )?(?:айсберг|романо))', query):
        return 'leaf_salad'
    # Explicit derivatives and prepared recipes remain distinct requests.
    if re.search(r'\b(?:мук\w*|хлопь\w*|хлеб\w*|лапш\w*|макарон\w*|каш\w*|готов\w*|варен\w*|тушен\w*|молоч\w*|суп\w*)\b', query):
        return None
    if re.search(r'\b(?:гречк\w*|гречнев\w*|гречих\w*|ядриц\w*|продел\w*)\b', query) and not re.search(r'\bс\b', query):
        return 'buckwheat'
    if re.search(r'\bрис(?:а)?\b', query) and not re.search(r'\bс\b', query):
        return 'rice'
    return None


def matches_ingredient(query, name, category=''):
    kind = ingredient_kind(query)
    if sugar_free(query) and not sugar_free(name):
        return False
    if 'безлактоз' in normalized(query) and 'безлактоз' not in normalized(name):
        return False
    if kind is None:
        return True
    name, category = normalized(name), normalized(category)
    if kind == 'cola':
        return bool(re.search(r'\bкола\b|\b(?:coca[- ]?cola|coolcola|cola)\b', name)) and not re.search(
            r'конфет|мармелад|шоколад|морожен|жевател', name + ' ' + category)
    if kind == 'potato_chips':
        return bool(re.search(r'\bчипс\w*\b', name) and re.search(r'картофел', name))
    if kind == 'milk':
        return bool(re.search(r'\bмолок[оа]\b', name)) and not re.search(
            r'растительн|миндаль|кокос|овсян|соев|сухое|сгущенн', name)
    if kind == 'leaf_salad':
        for variety in ('айсберг', 'романо'):
            if variety in normalized(query) and variety not in name:
                return False
        if re.search(r'готовая еда|детское питание|молочн|мясо|рыба|семена', category):
            return False
        return bool(re.search(r'\b(?:салат\w*|латук\w*|айсберг|руккол\w*)\b', name)) and (
            bool(re.search(r'овощ|зелень', category)) or
            bool(re.search(r'листов\w*|горшоч\w*|латук\w*|айсберг|руккол\w*|романо|афицион', name)))
    if re.search(r'готовая еда|детское питание|молочн|хлеб|выпечка|торты|пирожные|мясо|рыба', category):
        return False
    if re.search(r'\b(?:мук\w*|хлопь\w*|хлеб\w*|лапш\w*|макарон\w*|каш\w*|суп\w*|котлет\w*|гуляш\w*|тушен\w*|йогурт\w*|мед|напит\w*|молок\w*|крекер\w*|печень\w*|бисквит\w*)\b', name):
        return False
    pattern = r'\b(?:гречк\w*|гречнев\w*|гречих\w*|ядриц\w*|продел\w*)\b' if kind == 'buckwheat' else r'\bрис\w*\b'
    return bool(re.search(pattern, name))
