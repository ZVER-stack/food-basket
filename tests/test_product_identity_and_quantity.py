import json
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from foodai.agents.basket import BasketAgent
from foodai.services.pricing import calculate_basket


@pytest.fixture
def ingredients(catalog):
    template = catalog.products[('samokat:msk_center', 'msk_center', 'samokat_milk')]
    catalog.by_zone['msk_center'] = [template]
    rows = [
        ('leaf', 'Салат Афицион в горшочке, 90 г', 'Овощи, фрукты, зелень', '90', '75'),
        ('prepared_salad', 'Салат Лобио, 150 г', 'Готовая еда', '150', '50'),
        ('flour', 'Мука Гарнец из зеленой гречки, 500 г', 'Бакалея', '500', '136'),
        ('grain', 'Крупа гречневая ядрица, 800 г', 'Бакалея', '800', '100'),
        ('cooked', 'Гречка с тушёнкой из говядины, 250 г', 'Готовая еда', '250', '60'),
        ('yogurt', 'Греческий йогурт 2%, 135 г', 'Молочные продукты', '135', '55'),
        ('potato_chips', 'Чипсы Хрустящий Картофель, 160 г', 'Орехи, снеки', '160', '100'),
        ('cleaner', 'Чистящая паста Елизар, 500 г', 'Бытовая химия', '500', '10'),
    ]
    for sku, name, category, quantity, price in rows:
        p = replace(template, sku=sku, name=name, category=category, package_qty=Decimal(quantity),
                    package_unit='g', price_rub=Decimal(price))
        catalog.products[(p.storefront_id, p.zone_id, p.sku)] = p
        catalog.by_zone[p.zone_id].append(p)
    return catalog


def add_drink(catalog, sku, name, price):
    template = catalog.products[('samokat:msk_center', 'msk_center', 'samokat_milk')]
    p = replace(template, sku=sku, name=name, category='Вода, соки, напитки',
                package_qty=Decimal('500'), package_unit='ml', price_rub=Decimal(price))
    catalog.products[(p.storefront_id, p.zone_id, p.sku)] = p
    catalog.by_zone[p.zone_id].append(p)


def test_five_product_spoken_list_accepts_packs_decimal_comma_and_sugar_free():
    rows = BasketAgent.explicit_list('купи килограмм гречки, 2 штуки салата, литр молока, 3 пачки чипсов и колу без сахара 1,5 л')
    assert [(r['query'], r['quantity'], r['unit']) for r in rows] == [
        ('гречка', '1000', 'g'), ('салат', '2', 'pack'), ('молоко', '1000', 'ml'),
        ('чипсы', '3', 'pack'), ('кола без сахара', '1500.0', 'ml')]


def test_chips_are_food_and_sugar_free_cola_matches_zero_without_other_drinks(ingredients):
    add_drink(ingredients, 'zero', 'Газированный напиток Coca-Cola Zero, 0,5 л', '100')
    add_drink(ingredients, 'sugar', 'Кола Добрый, 0,5 л', '50')
    add_drink(ingredients, 'almond', 'Миндальный напиток без сахара, 0,5 л', '20')
    assert [p['sku'] for p in ingredients.search_products('чипсы', ['msk_center'])['samokat:msk_center']] == ['potato_chips']
    rows = ingredients.search_products('кола без сахара', ['msk_center'], {'required_terms': ['без сахара']})
    assert [p['sku'] for p in rows['samokat:msk_center']] == ['zero']
    req = [{'id': 'c', 'query': 'кола без сахара', 'quantity': '1500', 'unit': 'ml'}]
    bad = calculate_basket(req, [{'requirement_id': 'c', 'sku': 'sugar'}], 'samokat:msk_center', ingredients, 'msk_center')
    assert bad['missing'] == ['кола без сахара'] and not bad['lines']


def test_shortlist_compares_full_litre_cost_before_limiting_candidates(ingredients):
    template = ingredients.products[('samokat:msk_center', 'msk_center', 'samokat_milk')]
    for index in range(7):
        p = replace(template, sku=f'small_{index}', name=f'Молоко питьевое {index}, 100 мл',
                    package_qty=Decimal('100'), price_rub=Decimal(20+index))
        ingredients.products[(p.storefront_id, p.zone_id, p.sku)] = p
        ingredients.by_zone[p.zone_id].append(p)
    rows = ingredients.search_products('молоко', ['msk_center'], {'unit': 'ml', 'quantity': '1000'}, limit=1)
    assert rows['samokat:msk_center'][0]['sku'] == 'samokat_milk'


async def test_repeated_model_search_finishes_partial_cart_without_losing_other_products(ingredients):
    add_drink(ingredients, 'zero', 'Газированный напиток Coca-Cola Zero, 0,5 л', '100')
    ingredients.by_zone['msk_center'] = [p for p in ingredients.by_zone['msk_center'] if p.sku != 'grain']
    llm = AsyncMock()
    def repeat(messages, tools):
        data = json.loads(messages[1]['content'])
        assert data['phase'] == 'selection'
        grain = next(r for r in data['requirements'] if r['query'] == 'гречка')
        args = {'queries': [{'requirement_id': grain['id'], 'query': 'гречневая крупа'}]}
        return {'role': 'assistant', 'tool_calls': [{'id': 'again', 'type': 'function', 'function': {
            'name': 'search_products', 'arguments': json.dumps(args)}}]}
    llm.complete.side_effect = repeat
    agent = BasketAgent(ingredients, llm)
    context = {'zone_id': 'msk_center'}
    result = await agent.run(
        'купи килограмм гречки, 2 штуки салата, литр молока, 3 пачки чипсов и колу без сахара 1,5 л',
        context)
    assert result.status == 'need_clarification' and 'Какой салат' in result.text
    assert llm.complete.call_count == 0
    result = await agent.run('любой листовой', context)
    assert result.status == 'need_clarification' and '3 уп.' in result.text
    assert llm.complete.call_count == 0
    result = await agent.run('да', context)
    basket = next(b for b in result.structured['comparison']['partial'] if b['storefront_id'] == 'samokat:msk_center')
    assert basket['missing'] == ['гречка'] and len(basket['lines']) == 4
    lines = {r['sku']: r for r in basket['lines']}
    assert lines['leaf']['packages'] == 2 and lines['potato_chips']['packages'] == 3 and lines['zero']['packages'] == 3
    assert result.status == 'partial' and 'Не удалось завершить' not in result.text
    assert llm.complete.call_count <= 3 and result.structured['selection_fallback']


async def test_local_recovery_never_guesses_unclassified_product(ingredients):
    llm = AsyncMock()
    llm.complete.return_value = {'role': 'assistant', 'content': 'Всё найдено'}
    state = {'zone_id': 'msk_center', 'rules_version': 3,
             'draft': [{'id': 'unknown', 'query': 'неопознанный продукт', 'quantity': '1', 'unit': 'pack'}],
             'requirements': [{'id': 'unknown', 'query': 'неопознанный продукт', 'quantity': '1', 'unit': 'pack'}]}
    result = await BasketAgent(ingredients, llm).run('подбери недорого', {'zone_id': 'msk_center', 'basket_state': state})
    assert all(not b['lines'] for b in result.structured['comparison']['partial'])


def test_local_recovery_does_not_ignore_unhandled_dietary_constraint(ingredients):
    add_drink(ingredients, 'zero', 'Газированный напиток Coca-Cola Zero, 0,5 л', '100')
    state = {'zone_id': 'msk_center', 'requirements': [
        {'id': 'cola', 'query': 'кола без сахара и без кофеина', 'quantity': '1500', 'unit': 'ml'}]}
    result = BasketAgent(ingredients, None).finish_verified_candidates(state, 'msk_center', {}, [], 'test')
    assert all(not b['lines'] for b in result.structured['comparison']['partial'])


def test_spoken_list_and_named_quantity_reply_keep_physical_units():
    items = BasketAgent.explicit_list('салат 2 штуки, молоко литр и кило гречки')
    assert [(r['query'], r['quantity'], r['unit']) for r in items] == [
        ('салат', '2', 'pack'), ('молоко', '1000', 'ml'), ('гречка', '1000', 'g')]
    draft = [r if r['query'] != 'гречка' else r | {'quantity': None, 'unit': None} for r in items]
    updates = BasketAgent.quantity_reply('килограмм гречки', draft)
    assert len(updates) == 1 and updates[0]['quantity'] == '1000' and updates[0]['unit'] == 'g'
    assert updates[0]['id'] == draft[-1]['id']
    assert draft[1]['quantity'] == '1000' and draft[1]['unit'] == 'ml'


def test_basic_ingredient_search_rejects_derivatives_and_recognizes_grain_synonym(ingredients):
    grain = ingredients.search_products('гречка', ['msk_center'])['samokat:msk_center']
    assert [p['sku'] for p in grain] == ['grain']
    salad = ingredients.search_products('салат', ['msk_center'])['samokat:msk_center']
    assert [p['sku'] for p in salad] == ['leaf']
    flour = ingredients.search_products('мука из гречки', ['msk_center'])['samokat:msk_center']
    assert any(p['sku'] == 'flour' for p in flour)


def test_rephrased_search_cannot_offer_flour_for_original_grain_requirement(ingredients):
    agent = BasketAgent(ingredients, None)
    state = {'requirements': [{'id': 'grain', 'query': 'гречка', 'quantity': '1000', 'unit': 'g'}]}
    found = {}
    result = agent._execute('search_products', {'queries': [{'requirement_id': 'grain', 'query': 'мука из гречки'}]},
                            state, 'msk_center', found)
    assert all(p['sku'] != 'flour' for rows in result['results']['grain'].values() for p in rows)
    assert 'flour' not in found.get(('grain', 'samokat:msk_center'), set())


def test_calculator_independently_rejects_wrong_ingredient_and_counts_full_kilogram(ingredients):
    reqs = [{'id': 'b', 'query': 'гречка', 'quantity': '1000', 'unit': 'g'}]
    bad = calculate_basket(reqs, [{'requirement_id': 'b', 'sku': 'flour'}], 'samokat:msk_center', ingredients, 'msk_center')
    assert bad['missing'] == ['гречка'] and not bad['lines'] and bad['total'] == '0'
    good = calculate_basket(reqs, [{'requirement_id': 'b', 'sku': 'grain'}], 'samokat:msk_center', ingredients, 'msk_center')
    assert good['lines'][0]['packages'] == 2 and good['total'] == '200'


async def test_free_form_model_cannot_turn_litre_into_millilitre_and_solid_salad_is_two_packs(ingredients):
    llm = AsyncMock()
    def respond(messages, tools):
        data = json.loads(messages[1]['content'])
        if data.get('phase') != 'selection':
            name = 'set_requirements'
            args = {'replace_all': True, 'items': [
                {'id': 's', 'query': 'салат', 'quantity': '2', 'unit': 'pcs'},
                {'id': 'm', 'query': 'молоко', 'quantity': '1', 'unit': 'ml'},
                {'id': 'b', 'query': 'гречка', 'quantity': '1', 'unit': 'g'}]}
        else:
            assert [(r['quantity'], r['unit']) for r in data['requirements']] == [('2','pack'),('1000','ml'),('1000','g')]
            name = 'calculate_basket'
            args = {'baskets': [{'storefront_id': 'samokat:msk_center', 'items': [
                {'requirement_id': 's', 'sku': 'leaf'},
                {'requirement_id': 'm', 'sku': 'samokat_milk'},
                {'requirement_id': 'b', 'sku': 'grain'}]}]}
        return {'role': 'assistant', 'tool_calls': [{'id': name, 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(args)}}]}
    llm.complete.side_effect = respond
    agent = BasketAgent(ingredients, llm)
    context = {'zone_id': 'msk_center'}
    result = await agent.run('салат 2 штуки, молоко литр и кило гречки; подбери недорого', context)
    assert result.status == 'need_clarification' and 'Какой салат' in result.text
    result = await agent.run('любой листовой', context)
    basket = next(b for b in result.structured['comparison']['full'] if b['storefront_id'] == 'samokat:msk_center')
    lines = {row['sku']: row for row in basket['lines']}
    assert lines['leaf']['packages'] == 2 and lines['samokat_milk']['packages'] == 1 and lines['grain']['packages'] == 2
    assert 'flour' not in lines and llm.complete.call_count == 2


def test_no_grain_candidates_means_missing_instead_of_flour(ingredients):
    ingredients.by_zone['msk_center'] = [p for p in ingredients.by_zone['msk_center'] if p.sku != 'grain']
    assert ingredients.search_products('гречка', ['msk_center']) == {}
