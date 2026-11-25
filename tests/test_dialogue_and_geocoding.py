import json
from unittest.mock import AsyncMock, patch
import httpx
from foodai.agents.basket import BasketAgent
from foodai.services.pricing import calculate_basket
from foodai.services.geocoding import PhotonGeocoder
from foodai.services.address import AddressService


def call(items, replace=True):
    return {'role': 'assistant', 'tool_calls': [{'id': 'set', 'type': 'function', 'function': {
        'name': 'set_requirements', 'arguments': json.dumps({'items': items, 'replace_all': replace})}}]}


async def test_partial_cart_survives_clarification_and_model_cannot_drop_milk(catalog):
    llm = AsyncMock()
    agent = BasketAgent(catalog, llm)
    context = {'zone_id': 'msk_center'}
    draft = [{'id': key, 'query': query, 'quantity': None, 'unit': None}
             for key, query in [('s', 'салат'), ('m', 'молоко'), ('b', 'хлеб')]]
    llm.complete.return_value = call(draft)
    result = await agent.run('Салат, молоко, хлеб', context)
    assert result.status == 'need_clarification'
    assert len(context['basket_state']['draft']) == 3
    llm.complete.side_effect = [call([draft[0] | {'query': 'пекинская капуста', 'quantity': '1', 'unit': 'pack'},
                                   draft[2] | {'quantity': '1', 'unit': 'pack'}]),
                                {'role': 'assistant', 'content': 'Всё есть в наличии, доставка подтверждена.'}]
    result = await agent.run('Штука и буханка', context)
    assert result.status == 'need_clarification'
    assert 'молоко' in result.text
    assert len(context['basket_state']['draft']) == 3
    llm.complete.side_effect = None
    llm.complete.return_value = {'role': 'assistant', 'content': 'Всё есть в наличии, доставка подтверждена.'}
    result = await agent.run('Где покупать?', context)
    assert 'доставка подтверждена' not in result.text


def test_pack_counts_do_not_require_invented_weights(catalog):
    reqs = [{'id': 'milk', 'query': 'молоко', 'quantity': '2', 'unit': 'pack'}]
    result = calculate_basket(reqs, [{'requirement_id': 'milk', 'sku': 'samokat_milk'}],
                              'samokat:msk_center', catalog, 'msk_center')
    assert result['complete'] and result['lines'][0]['packages'] == 2
    assert result['total'] == '220'


def test_concrete_variety_with_new_model_id_retains_old_quantity_and_milk(catalog):
    agent = BasketAgent(catalog, None)
    state = {'_allow_replace': False, 'draft': [
        {'id': 'salad', 'query': 'салат', 'quantity': '1', 'unit': 'pack'},
        {'id': 'milk', 'query': 'молоко', 'quantity': '1000', 'unit': 'ml'},
        {'id': 'bread', 'query': 'хлеб', 'quantity': '1', 'unit': 'pack'}]}
    agent._execute('set_requirements', {'replace_all': True, 'items': [
        {'id': 'new_id', 'query': 'пекинская капуста', 'quantity': None, 'unit': None},
        {'id': 'wrong_id', 'query': 'хлеб обычный', 'quantity': None, 'unit': None}]}, state, 'msk_center', {})
    assert len(state['draft']) == 3
    assert state['draft'][0]['query'] == 'пекинская капуста'
    assert state['draft'][0]['quantity'] == '1'
    assert state['draft'][1]['quantity'] == '1000'
    assert state['draft'][2]['quantity'] == '1'
    assert BasketAgent.type_reply('Замени молоко на безлактозное', state['draft']) is None
    quantities = BasketAgent.quantity_reply('1 шт, литр, буханка', state['draft'])
    assert [(q['quantity'], q['unit']) for q in quantities] == [('1', 'pack'), ('1000', 'ml'), ('1', 'pack')]


async def test_geocoder_validates_exact_house_and_caches_requests(tmp_path):
    requests = []
    def response(request):
        requests.append(request)
        return httpx.Response(200, json={'features': [
            {'properties': {'street': 'Тверская улица', 'housenumber': '9'}, 'geometry': {'coordinates': [37.6, 55.7]}},
            {'properties': {'street': 'Тверская улица', 'housenumber': '7'}, 'geometry': {'coordinates': [37.61, 55.76]}}]})
    real = httpx.AsyncClient
    with patch('foodai.services.geocoding.httpx.AsyncClient', side_effect=lambda **kw: real(**kw, transport=httpx.MockTransport(response))):
        geocoder = PhotonGeocoder(tmp_path / 'geo.sqlite')
        first = await geocoder.lookup('ул. Тверская, д. 7, кв. 55')
        second = await geocoder.lookup('ул. Тверская, д. 7, кв. 55')
    assert first == second and len(requests) == 1
    assert first[0]['address'] == 'Москва, Тверская улица, 7'
    assert '55' not in requests[0].url.params['q']


async def test_moskvorechye_is_not_mistaken_for_moscow_and_corpus_is_normalized(tmp_path):
    requests = []
    def response(request):
        requests.append(request)
        return httpx.Response(200, json={'features': [
            {'properties': {'street': 'улица Москворечье', 'housenumber': '2 к3'}, 'geometry': {'coordinates': [37.6, 55.7]}},
            {'properties': {'street': 'улица Москворечье', 'housenumber': '2 к2'}, 'geometry': {'coordinates': [37.61, 55.71]}}]})
    real = httpx.AsyncClient
    with patch('foodai.services.geocoding.httpx.AsyncClient', side_effect=lambda **kw: real(**kw, transport=httpx.MockTransport(response))):
        geocoder = PhotonGeocoder(tmp_path / 'geo.sqlite')
        result = await geocoder.lookup('Москворечье 2к2')
        again = await geocoder.lookup('Москва, Москворечье 2 корпус 2')
    assert requests[0].url.params['q'] == 'Москва, Москворечье 2 к2'
    assert len(requests) == 1 and result == again
    assert result == [{'address': 'Москва, улица Москворечье, 2 к2', 'lon': 37.61, 'lat': 55.71}]


async def test_low_ranked_exact_corpus_is_found_by_bounded_fallback(tmp_path):
    requests = []
    def feature(house, city='Москва'):
        return {'properties': {'street': 'улица Москворечье', 'housenumber': house, 'city': city},
                'geometry': {'coordinates': [37.6495659, 55.6457851]}}
    def response(request):
        requests.append(request)
        return httpx.Response(200, json={'features': [feature('47 к2')] if len(requests) == 1 else
            [feature('2 к2', 'Домодедово'), feature('2 с2'), feature('3 к2'), feature('2 к2')]})
    real = httpx.AsyncClient
    with patch('foodai.services.geocoding.httpx.AsyncClient', side_effect=lambda **kw: real(**kw, transport=httpx.MockTransport(response))):
        geocoder = PhotonGeocoder(tmp_path / 'geo.sqlite')
        result = await geocoder.lookup('Москворечье 2 корпус 2')
        again = await geocoder.lookup('Москворечье 2к2')
    assert len(requests) == 2 and result == again
    assert requests[1].url.params['q'] == 'улица москворечье 2'
    assert requests[1].url.params['limit'] == '50'
    assert result == [{'address': 'Москва, улица Москворечье, 2 к2', 'lon': 37.6495659, 'lat': 55.6457851}]


async def test_house_and_corpus_numbers_cannot_be_swapped(tmp_path):
    def response(request):
        return httpx.Response(200, json={'features': [
            {'properties': {'street': 'улица Москворечье', 'housenumber': '3 к2'},
             'geometry': {'coordinates': [37.6, 55.7]}}]})
    real = httpx.AsyncClient
    with patch('foodai.services.geocoding.httpx.AsyncClient', side_effect=lambda **kw: real(**kw, transport=httpx.MockTransport(response))):
        assert await PhotonGeocoder(tmp_path / 'geo.sqlite').lookup('Москворечье 2 к3') == []


async def test_partial_house_suggestions_need_confirmation_and_do_not_assign_zone(catalog, tmp_path):
    requests = []
    def response(request):
        requests.append(request)
        return httpx.Response(200, json={'features': [
            {'properties': {'street': 'Каширское шоссе', 'housenumber': house, 'city': city},
             'geometry': {'coordinates': [37.6284234, 55.670806]}}
            for house, city in [('5', 'Домодедово'), ('5 к1', 'Москва'), ('57 к5', 'Москва'), ('5 к2 с6', 'Москва')]]})
    real = httpx.AsyncClient
    with patch('foodai.services.geocoding.httpx.AsyncClient', side_effect=lambda **kw: real(**kw, transport=httpx.MockTransport(response))):
        service = AddressService(catalog, geocoder=PhotonGeocoder(tmp_path / 'geo.sqlite'))
        result = await service.resolve_user_address('Каширское шоссе 5')
        again = await service.resolve_user_address('Каширское шоссе 5')
    assert len(requests) == 2 and result == again
    assert result['status'] == 'need_house_details' and result['zone_id'] is None
    assert result['suggestions'] == ['Москва, Каширское шоссе, 5 к1', 'Москва, Каширское шоссе, 5 к2 с6']


async def test_shared_pin_and_native_location_have_same_coverage_and_no_network(catalog):
    geocoder = AsyncMock()
    service = AddressService(catalog, geocoder=geocoder)
    service.geo = {'features': [{'properties': {'zone_id': 'msk_center'},
        'geometry': {'type': 'Polygon', 'coordinates': [[[37,55.5], [38,55.5], [38,56], [37,56], [37,55.5]]]}}]}
    link = 'https://maps.google.com/maps?q=55.76,37.61&ll=44.384147,-53.747309&z=16'
    assert (await service.resolve_user_address(link))['zone_id'] == 'msk_center'
    assert (await service.resolve_user_address(lat=55.76, lon=37.61))['zone_id'] == 'msk_center'
    assert (await service.resolve_user_address('https://maps.google.com/maps?q=44.384147,-53.747309'))['status'] == 'outside_moscow'
    assert (await service.resolve_user_address(lat=44.384147, lon=-53.747309))['status'] == 'outside_moscow'
    assert (await service.resolve_user_address(lat=55.2, lon=37.61))['status'] == 'outside_coverage'
    assert (await service.resolve_user_address(lat=float('nan'), lon=37.61))['status'] == 'invalid_location'
    assert (await service.resolve_user_address(lat=155, lon=37.61))['status'] == 'invalid_location'
    geocoder.lookup.assert_not_called()


async def test_house_coordinates_select_polygon_and_ambiguous_address_is_not_guessed(catalog):
    geocoder = AsyncMock()
    service = AddressService(catalog, geocoder=geocoder)
    service.geo = {'features': [{'properties': {'zone_id': 'msk_center'},
        'geometry': {'type': 'Polygon', 'coordinates': [[[37,55], [38,55], [38,56], [37,56], [37,55]]]}}]}
    geocoder.lookup.return_value = [{'address': 'Москва, Тверская улица, 7', 'lat': 55.76, 'lon': 37.61}]
    result = await service.resolve_user_address('Тверская 7')
    assert result['zone_id'] == 'msk_center' and result['user_address'] == 'Москва, Тверская улица, 7'
    geocoder.lookup.return_value.append({'address': 'Москва, Другая улица, 7', 'lat': 55.7, 'lon': 37.5})
    assert (await service.resolve_user_address('неясный адрес 7'))['status'] == 'ambiguous'
    geocoder.lookup.return_value = None
    assert (await service.resolve_user_address('Тверская 7'))['status'] == 'unavailable'


async def test_typed_variety_preserves_quantity_and_followup_price_needs_no_model(catalog):
    from dataclasses import replace
    template = catalog.products[('samokat:msk_center', 'msk_center', 'samokat_milk')]
    for sku, name in [('cabbage', 'Пекинская капуста'), ('bread', 'Хлеб обычный')]:
        product = replace(template, sku=sku, name=name, package_unit='g')
        catalog.products[(product.storefront_id, product.zone_id, sku)] = product
        catalog.by_zone['msk_center'].append(product)
    llm = AsyncMock()
    draft = [{'id': 's', 'query': 'салат', 'quantity': '1', 'unit': 'pack'},
             {'id': 'm', 'query': 'молоко', 'quantity': '1000', 'unit': 'ml'},
             {'id': 'b', 'query': 'хлеб', 'quantity': '1', 'unit': 'pack'}]
    def tool(name, args):
        return {'role': 'assistant', 'tool_calls': [{'id': name, 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(args)}}]}
    llm.complete.side_effect = [tool('search_products', {'queries': [
        {'requirement_id': r['id'], 'query': q} for r, q in zip(draft, ['пекинская капуста', 'молоко', 'хлеб обычный'])]}),
        tool('calculate_basket', {'baskets': [{'storefront_id': 'samokat:msk_center', 'items': [
            {'requirement_id': rid, 'sku': sku} for rid, sku in [('s', 'cabbage'), ('m', 'samokat_milk'), ('b', 'bread')]]}]})]
    context = {'zone_id': 'msk_center', 'basket_state': {'zone_id': 'msk_center', 'draft': draft}}
    agent = BasketAgent(catalog, llm)
    result = await agent.run('пекинская капуста, хлеб обычный', context)
    assert result.status == 'ok'
    assert [(r['quantity'], r['unit']) for r in result.structured['requirements']] == [('1', 'pack'), ('1000', 'ml'), ('1', 'pack')]
    assert llm.complete.call_count == 2
    again = await agent.run('А денег сколько?', context)
    assert again.text == result.text and llm.complete.call_count == 2


async def test_recent_answers_keep_question_context_but_not_house_address(catalog):
    llm = AsyncMock()
    llm.complete.return_value = {'role': 'assistant', 'content': 'Заказ доставят в любой дом.'}
    result = await BasketAgent(catalog, llm).run('Где покупать?', {'zone_id': 'msk_center',
        'history': [{'user': 'молоко', 'assistant': '📍 Москва, Тверская улица, 7\nСколько молока нужно?'}]})
    context = json.loads(llm.complete.call_args.args[0][1]['content'])
    assert context['recent_dialogue'][0]['assistant'] == 'Сколько молока нужно?'
    assert 'Тверская' not in json.dumps(context, ensure_ascii=False)
    assert 'доставят' not in result.text
