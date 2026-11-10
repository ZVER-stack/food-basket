"""Read explicit webPrice fields used by the original project parser."""
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import re
import ozon_fresh_fast as core
import ozon_har_catalog as tiles
from ozon_product_redirect import product_request


class MissingAmountError(core.ParseError):
    """This SKU is incomplete; it must not terminate unrelated products."""
    def __init__(self, message, item):
        super().__init__(message)
        self.item=deepcopy(item)


def product_prices(data, expected_name):
    heading = tiles.widget(data, 'webProductHeading-') or {}
    normalize = lambda text: re.sub(r'\s+', ' ', str(text)).strip().casefold()
    if normalize(heading.get('title', '')) != normalize(expected_name):
        raise core.ParseError('Product heading differs from the requested catalog item')
    candidates = []
    for key, value in tiles.widgets(data).items():
        # Exclude webPriceDecreasedCompact and prices in recommended products.
        if key.startswith('webPrice-') and isinstance(value, dict) and value.get('price'):
            current = core.number(value['price'])
            regular = core.number(value['originalPrice']) if value.get('originalPrice') else current
            card = core.number(value['cardPrice']) if value.get('cardPrice') else None
            if float(current) <= 0 or float(regular) < float(current) or (card is not None and float(card) <= 0):
                raise core.ParseError('Invalid product webPrice values')
            candidates.append((current, regular, card))
    if not candidates or len(set(candidates)) != 1:
        raise core.ParseError('Missing or conflicting product webPrice fields')
    current, regular, card = candidates[0]
    return {'price': current, 'price_regular': regular, 'price_card': card}


def fresh_product_offer(data, expected_sku):
    buttons = [value.get('firstButton', {}) for key,value in tiles.widgets(data).items()
               if key.startswith('webAddToCart-') and isinstance(value, dict)]
    for button in buttons:
        action = button.get('toCart', {})
        params = action.get('action', {}).get('params', {})
        if (action.get('testInfo', {}).get('automatizationId') == 'saleblock-add-to-cart-button-express'
                and action.get('action', {}).get('id') == 'addToCart'
                and str(button.get('sku')) == str(expected_sku)
                and str(params.get('sku')) == str(expected_sku)):
            return True
    raise core.ParseError('Product page has no matching Fresh offer for the requested SKU')


def product_unit_quote(data):
    quotes=set()
    for key,value in tiles.widgets(data).items():
        if key.startswith('webPrice-') and value.get('pricePerUnit') and value.get('measurePerUnit'):
            basis=str(value['measurePerUnit']).removeprefix('за ').strip()
            if tiles.full_amount(basis) is None:
                raise core.ParseError('Product unit price has an unknown basis')
            quotes.add((core.number(value['pricePerUnit']),basis))
    if len(quotes)!=1:
        raise core.ParseError('Missing or conflicting product unit-price quote')
    return next(iter(quotes))


def product_amount(data):
    """Literal primary product characteristics, never mass-based price math."""
    units = {
        'вес товара, г': (0, 'г'), 'вес нетто, г': (0, 'г'),
        'вес товара, кг': (0, 'кг'), 'вес нетто, кг': (0, 'кг'),
        'объем, мл': (1, 'мл'), 'объем, л': (1, 'л'),
        'количество в упаковке, шт': (2, 'шт'),
        'количество в упаковке, шт.': (2, 'шт'),
        'общее количество капсул/таблеток': (2, 'шт'),
    }
    found=[]
    def visit(node):
        if isinstance(node, list):
            for value in node:visit(value)
        elif isinstance(node, dict):
            title=node.get('title')
            if isinstance(title,dict):
                title=''.join(part.get('content','') for part in title.get('textRs',[]) if part.get('type')=='text')
            if isinstance(title,str):
                normalized=re.sub(r'\s+',' ',title).strip().casefold()
                if normalized in units:
                    values=node.get('values',[])
                    if len(values)==1 and isinstance(values[0].get('text'),str):
                        literal=values[0]['text'].strip()
                        try:numeric=core.number(literal)
                        except core.ParseError:return
                        if float(numeric)>0:
                            rank,unit=units[normalized]
                            found.append((rank,f'{literal} {unit}',title,literal))
            for value in node.values():
                if isinstance(value,(dict,list)):visit(value)
    for key,value in tiles.widgets(data).items():
        if key.startswith(('webShortCharacteristics-','webCharacteristics-')):visit(value)
    if not found:raise core.ParseError('Product has no explicit displayed amount characteristic')
    rank=min(item[0] for item in found)
    best=[item for item in found if item[0]==rank]
    if len({item[1] for item in best})!=1:
        raise core.ParseError('Product amount characteristics disagree')
    _,amount,title,literal=best[0]
    return amount,{'source':'product_characteristics','title':title,'displayed_value':literal}


def product_metadata(data):
    """Read only primary product description/characteristics; missing stays missing."""
    from html import unescape
    aliases = {
        'бренд':'brand', 'производитель':'manufacturer', 'страна-изготовитель':'country',
        'страна изготовитель':'country', 'состав':'composition', 'срок годности':'shelf_life',
        'условия хранения':'storage_conditions', 'белки, г':'protein', 'жиры, г':'fat',
        'углеводы, г':'carbs', 'энергетическая ценность, ккал':'kcal',
        'белки, г/100 г':'protein', 'жиры, г/100 г':'fat', 'углеводы, г/100 г':'carbs',
        'энергетическая ценность, ккал/100 г':'kcal'}
    characteristics = {}; result = {}; descriptions = []
    def text(value):
        if isinstance(value, str): return value.strip()
        if isinstance(value, dict):
            if isinstance(value.get('text'), str): return value['text'].strip()
            return ''.join(x.get('content','') for x in value.get('textRs',[]) if x.get('type')=='text').strip()
        return ''
    def walk(node):
        if isinstance(node, list):
            for value in node: walk(value)
        elif isinstance(node, dict):
            title = text(node.get('title')); values = node.get('values')
            if title and isinstance(values, list):
                displayed = [text(v) for v in values]; displayed = [v for v in displayed if v]
                if displayed:
                    characteristics[title] = displayed
                    key = aliases.get(re.sub(r'\s+', ' ',title).casefold())
                    if key and len(displayed)==1:
                        value = displayed[0]
                        if key in ('protein','fat','carbs','kcal'):
                            if not re.fullmatch(r'\d+(?:[.,]\d+)?',value): value = None
                            else: value = core.nutrition_number(value)
                        if value is not None: result.setdefault(key,value)
            for value in node.values():
                if isinstance(value,(list,dict)): walk(value)
    def description(node):
        if isinstance(node, dict):
            for key,value in node.items():
                if key in ('html','text','content','description') and isinstance(value,str):
                    clean = re.sub(r'<[^>]*>', ' ',value)
                    clean = re.sub(r'\s+',' ',unescape(clean)).strip()
                    if clean: descriptions.append(clean)
                elif isinstance(value,(dict,list)): description(value)
        elif isinstance(node,list):
            for value in node: description(value)
    for key,value in tiles.widgets(data).items():
        if key.startswith(('webCharacteristics-', 'webShortCharacteristics-')): walk(value)
        elif key.startswith('webNutritionInfo-'):
            labels = {'белки':'protein','жиры':'fat','углеводы':'carbs','ккал':'kcal'}
            for nutrient in value.get('values',[]):
                field = labels.get(str(nutrient.get('label','')).strip().casefold())
                literal = str(nutrient.get('value','')).strip()
                if field and re.fullmatch(r'\d+(?:[.,]\d+)?',literal): result[field] = core.nutrition_number(literal)
        elif key.startswith('webDescription-'): description(value)
    if descriptions: result['description'] = '\n'.join(dict.fromkeys(descriptions))
    return result, characteristics


def enrich_prices(client, zone, item):
    data = product_request(client,item)
    observed_at = core.observed_now()
    try:
        result = _enrich_prices_data(data, zone, item, observed_at=observed_at)
        if (item.get('extra') or {}).get('product_details_required'):
            paginator = tiles.widget(data, 'paginator-') or {}
            link = paginator.get('nextPage')
            if link:
                target = urlsplit(core.public_url(link))
                original = urlsplit(core.public_url(item['url']))
                query = parse_qs(target.query)
                if (target.path != original.path or query.get('layout_page_index') != ['2'] or
                        not query.get('layout_container',[''])[0].startswith('pdp')):
                    raise core.ParseError('Product details continuation differs from the verified SKU')
                details = product_request(client,item,target.path + '?' + target.query)
                header = tiles.address_title(details)
                if header and tiles.normalized_address(header) != tiles.normalized_address(zone['address']):
                    raise core.ParseError('Product details changed the verified house')
                metadata, characteristics = product_metadata(details)
                result.update(metadata)
                result['extra']['product_characteristics'].update(characteristics)
                result['extra']['product_metadata_fields_observed'] = sorted(set(
                    result['extra']['product_metadata_fields_observed']) | set(metadata))
                result['extra']['product_metadata_observed_at'] = core.observed_now()
            result['extra']['product_metadata_schema'] = 2
        return result
    except core.ParseError as exc:
        state_path=getattr(client,'state_path',None)
        if isinstance(state_path,(str,Path)):
            # Preserve primary public fields before propagating the error.
            # Keep this local diagnostic outside exported artifacts.
            prefixes=('webProductHeading-','webPrice-','webAddToCart-',
                      'webCharacteristics-','webShortCharacteristics-','addressBookBarWeb-')
            primary={k:v for k,v in tiles.widgets(data).items() if k.startswith(prefixes)}
            core.write_json(Path(state_path).parent/'parse_failures'/f"product_{item['product_id']}.json",
                {'captured_at':core.now(),'error':str(exc),'zone':zone,'item':item,
                 'data':{'widgetStates':primary}})
        raise


def _enrich_prices_data(data, zone, item, observed_at=None):
    observed_at = observed_at or core.observed_now()
    address = tiles.address_title(data)
    if not address or tiles.normalized_address(address) != tiles.normalized_address(zone['address']):
        raise core.ParseError('Product page does not confirm the selected delivery house')
    fresh_product_offer(data, item['product_id'])
    prices = product_prices(data, item['name'])
    unit_quote=product_unit_quote(data) if item.get('unit_price_basis') else None
    if unit_quote and re.sub(r'\s+','',unit_quote[1])!=re.sub(r'\s+','',item['unit_price_basis']):
        raise core.ParseError('Catalog and product unit-price bases differ')
    result = deepcopy(item)
    result.update(prices)
    result['extra'].update(ordinary_price_source='product_webPrice',ordinary_price_verified_for_address=True,
                          ordinary_price_inferred=False,ordinary_price_observed_at=observed_at)
    if not result.get('amount'):
        title_amount=core.AMOUNT.findall(result.get('name') or '')
        if title_amount:
            result['amount']=title_amount[-1].strip()
            result['extra']['amount_origin']={'source':'matching_product_title'}
        else:
            try:result['amount'],result['extra']['amount_origin']=product_amount(data)
            except core.ParseError as exc:raise MissingAmountError(str(exc),result) from exc
    if any(item.get(key) is not None and item.get(key)!=prices[key] for key in prices):
        result['extra'].update(catalog_prices={key:item.get(key) for key in prices},
            catalog_product_prices_differ=True,selected_price_source='same_house_fresh_product_webPrice',
            product_prices_captured_at=observed_at)
    # All three price roles are taken together from this verified SKU/house.
    # If no product card price exists, do not retain a different catalog quote.
    result.update(prices)
    if unit_quote:
        # The catalog can quote per kg while the product page quotes a pack.
        # Read both displayed values; never multiply by an estimated weight.
        result['extra'].update(catalog_price_card=item.get('price_card'),
            catalog_unit_price=item.get('unit_price'),catalog_unit_price_basis=item['unit_price_basis'],
            price_basis='product_displayed',unit_price_source='webPrice.pricePerUnit',
            unit_price_kind='as_displayed_in_product')
        result['unit_price'],result['unit_price_basis']=unit_quote
    metadata, characteristics = product_metadata(data)
    from ozon_catalog_index import METADATA_FIELDS
    for field in METADATA_FIELDS:
        if field in metadata: result[field] = metadata[field]
    result['extra'].update(product_metadata_fields_observed=sorted(metadata),
                          product_characteristics=characteristics,
                          product_metadata_observed_at=observed_at, product_details_required=False)
    result['extra']['ordinary_price_source'] = 'product_webPrice'
    result['extra'].update(ordinary_price_verified_for_address=True,
                          ordinary_price_inferred=False, ordinary_price_observed_at=observed_at)
    return result
