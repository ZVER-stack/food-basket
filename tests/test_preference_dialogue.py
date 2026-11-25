from dataclasses import replace
from decimal import Decimal

from foodai.agents.basket import BasketAgent
from foodai.services.llm import MockLLM


def add(catalog, sku, name, quantity, unit, price, brand=''):
    template = catalog.products[('samokat:msk_center', 'msk_center', 'samokat_milk')]
    product = replace(template, sku=sku, name=name, package_qty=Decimal(quantity), package_unit=unit,
                      price_rub=Decimal(price), brand=brand, category='Орехи, снеки' if unit == 'g' else 'Напитки')
    catalog.products[(product.storefront_id, product.zone_id, sku)] = product
    catalog.by_zone[product.zone_id].append(product)


async def test_brand_edit_preserves_three_packs_and_other_items_and_asks_before_substitution(catalog):
    add(catalog, 'chips', 'Чипсы картофельные Другой бренд', '160', 'g', '100', 'Другой бренд')
    context = {'zone_id': 'msk_center', 'basket_state': {'zone_id': 'msk_center', 'draft': [
        {'id': 'c', 'query': 'чипсы', 'quantity': '3', 'unit': 'pack'},
        {'id': 'm', 'query': 'молоко', 'quantity': '1000', 'unit': 'ml'}]}}
    agent = BasketAgent(catalog, MockLLM())
    result = await agent.run('хочу чипсы lays', context)
    assert result.status == 'need_clarification' and 'Разрешить другой бренд' in result.text
    chips, milk = context['basket_state']['draft']
    assert chips == {'id': 'c', 'query': 'чипсы', 'quantity': '3', 'unit': 'pack', 'brand': 'Lay’s'}
    assert milk['quantity'] == '1000' and len(context['basket_state']['draft']) == 2
    result = await agent.run('нет', context)
    assert 'Lay’s — 3 уп.' in result.text and 'остались в списке' in result.text
    assert context['basket_state']['draft'][0]['brand'] == 'Lay’s'
    assert all('Lay' not in line['name'] for b in result.structured['comparison']['partial'] for line in b['lines'])


async def test_other_chip_brand_used_only_after_explicit_yes(catalog):
    add(catalog, 'chips', 'Чипсы картофельные Другой бренд', '160', 'g', '100', 'Другой бренд')
    context = {'zone_id': 'msk_center', 'basket_state': {'zone_id': 'msk_center', 'draft': [
        {'id': 'c', 'query': 'чипсы', 'quantity': '3', 'unit': 'pack'}]}}
    agent = BasketAgent(catalog, MockLLM())
    await agent.run('хочу чипсы lays', context)
    result = await agent.run('да', context)
    assert not context['basket_state']['draft'][0].get('brand')
    basket = next(b for b in result.structured['comparison']['full'] if b['storefront_id'] == 'samokat:msk_center')
    assert basket['lines'][0]['packages'] == 3


def test_lays_brand_spelling_is_exact_and_aliases_are_supported(catalog):
    add(catalog, 'lays', "Чипсы картофельные Lay's Сыр", '140', 'g', '150', "Lay’s")
    for spelling in ("Lay's", 'lays', 'лейс', 'лэйс'):
        rows = catalog.search_products('чипсы', ['msk_center'], {'brand': spelling})
        assert [p['sku'] for p in rows['samokat:msk_center']] == ['lays']


async def test_salad_kind_is_asked_and_quantity_is_kept(catalog):
    add(catalog, 'leaf', 'Салат Афицион в горшочке', '90', 'g', '75')
    agent = BasketAgent(catalog, MockLLM())
    context = {'zone_id': 'msk_center'}
    result = await agent.run('салат 2 штуки, молоко литр', context)
    assert result.status == 'need_clarification' and 'Какой салат' in result.text
    assert context['_usage']['model_calls'] == 0
    result = await agent.run('любой листовой', context)
    assert context['basket_state']['draft'][0]['query'] == 'салат листовой'
    assert context['basket_state']['draft'][0]['quantity'] == '2'
    assert any(line['sku'] == 'leaf' and line['packages'] == 2
               for b in result.structured['comparison']['full'] for line in b['lines'])


async def test_exact_cola_bottle_is_preferred_to_cheaper_small_cans(catalog):
    add(catalog, 'small_zero', 'Coca-Cola Zero 0,33 л', '330', 'ml', '40', 'Coca-Cola')
    add(catalog, 'large_zero', 'Coca-Cola Zero 1,5 л', '1500', 'ml', '250', 'Coca-Cola')
    result = await BasketAgent(catalog, MockLLM()).run('колу без сахара 1,5 л', {'zone_id': 'msk_center'})
    basket = next(b for b in result.structured['comparison']['full'] if b['storefront_id'] == 'samokat:msk_center')
    assert basket['lines'][0]['sku'] == 'large_zero' and basket['lines'][0]['packages'] == 1


async def test_small_cans_need_permission_and_no_keeps_requested_size(catalog):
    add(catalog, 'small_zero', 'Coca-Cola Zero 0,33 л', '330', 'ml', '40', 'Coca-Cola')
    agent = BasketAgent(catalog, MockLLM())
    context = {'zone_id': 'msk_center'}
    first = await agent.run('колу без сахара 1,5 л', context)
    assert first.status == 'need_clarification' and '5 уп.' in first.text and '1,65 л' in first.text
    assert context['_usage']['model_calls'] == 0
    denied = await agent.run('нет', context)
    assert all(not b['lines'] for b in denied.structured['comparison']['partial'])
    assert context['basket_state']['requirements'][0]['quantity'] == '1500.0'
    assert '1,5 л' in denied.text


async def test_small_cans_can_be_selected_after_yes(catalog):
    add(catalog, 'small_zero', 'Coca-Cola Zero 0,33 л', '330', 'ml', '40', 'Coca-Cola')
    agent = BasketAgent(catalog, MockLLM())
    context = {'zone_id': 'msk_center'}
    await agent.run('колу без сахара 1,5 л', context)
    result = await agent.run('да', context)
    basket = next(b for b in result.structured['comparison']['full'] if b['storefront_id'] == 'samokat:msk_center')
    assert basket['lines'][0]['sku'] == 'small_zero' and basket['lines'][0]['packages'] == 5
