from copy import deepcopy
import json
from unittest.mock import AsyncMock
import pytest
from foodai.agents.basket import BasketAgent
from foodai.application import MODES
from foodai.services.catalog import Catalog
from foodai.services.llm import LLMError
from foodai.services.pricing import calculate_basket, compare_stores
from foodai.services.units import normalize_quantity, parse_package

REQS = [{'id': 'milk', 'query': 'молоко', 'quantity': '1000', 'unit': 'ml'},
        {'id': 'eggs', 'query': 'яйца', 'quantity': '10', 'unit': 'pcs'}]


def basket(catalog, store, items):
    store += ':msk_center'
    return calculate_basket(REQS, [{'requirement_id': key, 'sku': sku} for key, sku in items],
                            store, catalog, 'msk_center')


def test_B1_and_B2_whole_packs(catalog):
    a = basket(catalog, 'lavka_small', [('milk', 'lavka_small_milk'), ('eggs', 'lavka_small_eggs')])
    b = basket(catalog, 'samokat', [('milk', 'samokat_milk'), ('eggs', 'samokat_eggs')])
    assert a['lines'][0]['packages'] == 2
    assert a['lines'][0]['subtotal'] == '180'
    assert a['total'] == '300'
    assert b['total'] == '240'
    assert compare_stores([a, b])['winner'] == 'samokat:msk_center'


def test_B3_partial_cannot_win_and_storefronts_are_separate(catalog):
    a = basket(catalog, 'samokat', [('milk', 'samokat_milk'), ('eggs', 'samokat_eggs')])
    c = basket(catalog, 'vkusvill', [('milk', 'vkusvill_milk')])
    big = basket(catalog, 'lavka_big', [('milk', 'lavka_big_milk'), ('eggs', 'lavka_small_eggs')])
    assert not big['complete'] and not c['complete']
    assert c['missing'] == ['яйца']
    assert compare_stores([a, c])['winner'] == a['storefront_id']
    assert compare_stores([c, big])['winner'] is None


async def test_B4_followup_preserves_eggs_and_calls_multiple_tools(app):
    await app.handle(1, 'msk_center')
    await app.handle(1, MODES['basket'])
    first = await app.handle(1, 'молоко 1 л, яйца 10 шт')
    second = await app.handle(1, 'Замени молоко на безлактозное')
    assert first.status == second.status == 'ok'
    assert [t['tool'] for t in first.structured['tool_trace']] == [
        'set_requirements', 'search_products', 'calculate_basket', 'compare_stores']
    before = first.structured['requirements'][1]
    assert before == second.structured['requirements'][1]
    assert 'безлактоз' in second.structured['requirements'][0]['query']
    assert all('безлактоз' in basket['lines'][0]['name'].lower()
               for basket in second.structured['comparison']['full'])


async def test_B5_zone_without_coverage_is_not_priced(catalog):
    catalog.manifest = {}
    agent = BasketAgent(catalog, AsyncMock())
    result = await agent.run('молоко 1 л', {'zone_id': 'msk_09'})
    assert result.status == 'no_data'
    agent.llm.complete.assert_not_called()


def test_B6_smoked_chicken_cannot_match_raw_fillet(catalog):
    results = catalog.search_products('филе курицы', ['msk_center'])
    assert results
    assert all('копч' not in p['name'].lower() for ps in results.values() for p in ps)


def test_search_limits_zones_brand_and_allergen_terms(catalog):
    results = catalog.search_products('молоко', ['msk_03'], {'required_terms': ['безлактоз']}, 1)
    assert all(len(values) <= 1 for values in results.values())
    assert all(p['zone_id'] == 'msk_03' and 'безлактоз' in p['name'].lower()
               for ps in results.values() for p in ps)
    assert catalog.search_products('молоко', ['msk_center'], {'exclude_brands': ['Тестовый бренд']}) == {}
    assert catalog.get_product_details('invented', 'samokat:msk_center', 'msk_center') is None


def test_hallucinated_sku_rejected_before_calculation(catalog):
    agent = BasketAgent(catalog, None)
    state = {'requirements': REQS}
    with pytest.raises(ValueError):
        agent._execute('calculate_basket', {'baskets': [{'storefront_id': 'samokat:msk_center',
            'items': [{'requirement_id': 'milk', 'sku': 'fabricated'}]}]}, state, 'msk_center', {})


async def test_tool_budget_stops_infinite_model(catalog):
    call = {'role': 'assistant', 'tool_calls': [{'id': 'again', 'type': 'function',
            'function': {'name': 'untrusted_python', 'arguments': '{}'}}]}
    llm = AsyncMock()
    llm.complete.return_value = call
    result = await BasketAgent(catalog, llm, max_tools=3).run('молоко', {'zone_id': 'msk_center'})
    assert result.status == 'partial'
    assert len(result.structured['tool_trace']) == 3
    assert all('error' in t['result'] for t in result.structured['tool_trace'])


async def test_llm_failure_is_structured(catalog):
    llm = AsyncMock()
    llm.complete.side_effect = LLMError('Временный сбой API')
    result = await BasketAgent(catalog, llm).run('молоко 1 л', {'zone_id': 'msk_center'})
    assert result.status == 'error'
    assert 'Traceback' not in result.text


@pytest.mark.parametrize('literal,quantity,unit', [('1.5 л','1500.0','ml'), ('900 г','900','g'),
                                                   ('10 шт.','10','pcs'), ('6 × 100 г','600','g')])
def test_declared_units(literal, quantity, unit):
    qty, parsed = parse_package(literal)
    assert qty == normalize_quantity(quantity, unit)[0] and parsed == unit


def test_unknown_and_incompatible_units_not_invented(catalog):
    with pytest.raises(ValueError):
        parse_package('примерно 1 кг')
    assert not basket(catalog, 'samokat', [('milk', 'samokat_eggs')])['complete']


def test_duplicate_ingredients_share_whole_packs(catalog):
    needs = [{'id': 'a', 'query': 'молоко', 'quantity': '400', 'unit': 'ml'},
             {'id': 'b', 'query': 'молоко', 'quantity': '400', 'unit': 'ml'}]
    result = calculate_basket(needs, [{'requirement_id': k, 'sku': 'samokat_milk'} for k in ('a', 'b')],
                              'samokat:msk_center', catalog, 'msk_center')
    assert result['lines'][0]['packages'] == 1
    assert result['total'] == '110'
