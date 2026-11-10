"""Strict public house search when the map resolves only the city."""
import math
from urllib.parse import parse_qs, quote, urlsplit

import ozon_fresh_fast as core
import ozon_har_catalog as tiles


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def address_field(data):
    form = tiles.widget(data, 'addressEditForm-') or {}
    if not isinstance(form, dict):
        raise core.ParseError('House-search form is malformed')
    fields = [v for v in _walk(form) if v.get('name') == 'addressTail']
    if len(fields) != 1:
        raise core.ParseError('Expected one public house-search field')
    return form, fields[0]


def house_search_required(data, zone=None):
    button = tiles.widget(data, 'addressEditConfirmButton-') or {}
    buttons = button.get('buttonsWeb', [])
    # The map can resolve a nearby numbered house despite an enabled button.
    # Use the existing exact Fresh suggestion/validation protocol in that case.
    if zone is not None and len(buttons) == 1 and buttons[0].get('text') == 'Привезти сюда':
        try:
            form, field = address_field(data)
        except core.ParseError:
            pass  # Existing strict map/final checks still apply without this field.
        else:
            value = field.get('value')
            if (form.get('isFresh') is True and form.get('type') == 'FORM_TYPE_SHORT'
                    and field.get('isEnabled') is True
                    and field.get('fieldType') == 'FIELD_TYPE_SEARCH_YANDEX'
                    and isinstance(value, str) and value.strip()
                    and tiles.normalized_address(value) != tiles.normalized_address(zone['address'])):
                return True
    if not (len(buttons) == 1 and buttons[0].get('text') == 'Привезти сюда'
            and buttons[0].get('disabled') is True and buttons[0].get('theme') == 'disabled'):
        return False
    form, field = address_field(data)
    return (form.get('isFresh') is True and form.get('type') == 'FORM_TYPE_SHORT'
            and field.get('isEnabled') is True and field.get('fieldType') == 'FIELD_TYPE_SEARCH_YANDEX'
            and any(v.get('text') == 'Укажите номер дома' for v in _walk(form)))


def _distance(point, zone):
    try:
        delta = core.distance(point['lat'], point['lon'], zone['address_lat'], zone['address_lon'])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise core.ParseError('House search did not provide valid coordinates') from None
    if not math.isfinite(delta) or delta > 100:
        raise core.ParseError('House search coordinates do not match the requested house')


def select_suggestion(response, zone):
    if not isinstance(response, dict):
        raise core.ParseError('House search response is not an object')
    if response.get('sourceQuery') != zone['address'] or not isinstance(response.get('suggestions'), list):
        raise core.ParseError('House search response does not match the requested query')
    matches = []
    expected = tiles.normalized_address(zone['address'])
    for index, suggestion in enumerate(response['suggestions']):
        if not isinstance(suggestion, dict) or not isinstance(suggestion.get('address'), dict):
            raise core.ParseError('House search suggestion is malformed')
        address = suggestion.get('address', {})
        text = address.get('fullText', '')
        if not isinstance(text, str) or tiles.normalized_address(text) != expected:
            continue
        components = address.get('components', {})
        if (not isinstance(components, dict)
                or any(not isinstance(components.get(k), dict) for k in ('city', 'street', 'house'))
                or not isinstance(address.get('geometry'), dict)):
            raise core.ParseError('House search identity or geometry is malformed')
        city = components.get('city', {}).get('name')
        street = components.get('street', {}).get('name')
        house = components.get('house', {}).get('number')
        if (address.get('isoAlpha2CountryCode') != 'RU' or city != 'Москва'
                or not isinstance(street, str) or not isinstance(house, str) or not house
                or tiles.normalized_address(f'{city}, {street}, {house}') != expected
                or suggestion.get('trustCoord') is not True or suggestion.get('emptyGeometry') is not False):
            raise core.ParseError('House search has conflicting house identity or untrusted geometry')
        point = address.get('geometry', {}).get('point', {})
        _distance(point, zone)
        identity = (city.casefold(), street.casefold(), tiles.normalized_address(house))
        matches.append((index, text, identity, point))
    if not matches:
        raise core.ParseError('No exact house suggestion matches the requested address')
    for candidate in matches[1:]:
        if candidate[2] != matches[0][2]:
            raise core.ParseError('Multiple distinct houses matched the search')
        _distance(candidate[3], {'address_lat': matches[0][3]['lat'], 'address_lon': matches[0][3]['lon']})
    # Ozon may publish the same house from two providers. Keep its original rank
    # only after both house identity and every point pass the same checks.
    return matches[0][0], matches[0][1]


def validation_request(data, zone, index, text):
    form, field = address_field(data)
    if (not isinstance(field.get('searchOptions'), dict)
            or not isinstance(field['searchOptions'].get('action'), dict)):
        raise core.ParseError('Published house-search action is malformed')
    template = field.get('searchOptions', {}).get('link', '')
    action_template = field.get('searchOptions', {}).get('action', {}).get('link')
    parts = urlsplit(template)
    query = parse_qs(parts.query)
    if (form.get('isFresh') is not True or form.get('type') != 'FORM_TYPE_SHORT'
            or parts.scheme or parts.netloc or parts.fragment or parts.path != 'validateCourierAddressV2'
            or action_template != template or query.get('miniapp') != ['supermarket']
            or query.get('status') != ['FORM_TYPE_SHORT'] or query.get('q') != ['{q}']
            or len(query.get('lat', [])) != 1 or len(query.get('long', [])) != 1):
        raise core.ParseError('Published Fresh house-search action changed')
    _distance({'lat': query.get('lat', [None])[0], 'lon': query.get('long', [None])[0]}, zone)
    return {'url': 'https://www.ozon.ru/api/composer-api.bx/_action/v2/' + template.replace('{q}', quote(zone['address'], safe='')),
            'method': 'POST', 'json': {'index': index, 'searchString': zone['address'],
                                     'suggestValue': text, 'status': form['type']}}


def validated_link(response, zone, selected_text):
    if not isinstance(response, dict) or not isinstance(response.get('data'), dict):
        raise core.ParseError('House validation response is malformed')
    result = response.get('data', {})
    action = result.get('action', {})
    if not isinstance(action, dict):
        raise core.ParseError('House validation action is malformed')
    link = action.get('link', '')
    if (result.get('isExact') is not True or result.get('isAvailable') is not True
            or action.get('behavior') != 'BEHAVIOR_TYPE_COMPOSER_NESTED_PAGE' or not link):
        raise core.ParseError('Ozon did not confirm an exact available house')
    core.public_url(link)
    parts = urlsplit(link)
    query = parse_qs(parts.query)
    if (parts.path != '/modal/fresh' or parts.fragment or query.get('miniapp') != ['supermarket']
            or query.get('available') != ['t'] or query.get('sie') != ['t'] or query.get('suggest') != ['t']
            or len(query.get('lat', [])) != 1 or len(query.get('long', [])) != 1
            or len(query.get('searchString', [])) != 1 or len(query.get('suggestValue', [])) != 1
            or tiles.normalized_address(query['searchString'][0]) != tiles.normalized_address(zone['address'])
            or query['suggestValue'][0] != selected_text):
        raise core.ParseError('Validated house action does not match the selected Fresh house')
    _distance({'lat': query.get('lat', [None])[0], 'lon': query.get('long', [None])[0]}, zone)
    return link


def resolve_house(client, data, zone):
    mapping = tiles.widget(data, 'addressEditMap-') or {}
    try:
        viewport = mapping['viewport']
        body = {'clientName': 'ozonWeb', 'query': zone['address'], 'count': 10, 'lang': 'ru',
                'contextLocationUID': mapping['locationUid'], 'removeDisallowedCountries': True,
                'bbox': {'leftLowerCorner': {'lat': viewport['leftBottom']['latitude'], 'lon': viewport['leftBottom']['longitude']},
                         'rightUpperCorner': {'lat': viewport['rightTop']['latitude'], 'lon': viewport['rightTop']['longitude']}}}
    except (KeyError, TypeError):
        raise core.ParseError('Published house-search map context is incomplete') from None
    suggestions = client.request({'url': 'https://www.ozon.ru/api/composer-api.bx/_action/suggest',
                                  'method': 'POST', 'json': body})
    index, text = select_suggestion(suggestions, zone)
    response = client.request(validation_request(data, zone, index, text))
    return validated_link(response, zone, text)
