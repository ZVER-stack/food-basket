import csv
from foodai.application import MODES
from foodai.agents.basket import BasketAgent
from foodai.handlers.bot import zone_keyboard
from foodai.services.address import AddressService, address_key
from foodai.services.catalog import ZONE_IDS
from foodai.services.local_input import prepare_items


def address_service(tmp_path, catalog):
    path = tmp_path / 'zones.csv'
    with path.open('w', encoding='utf8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['zone_id', 'zone_name', 'address'])
        writer.writeheader()
        writer.writerows({'zone_id': zone, 'zone_name': 'Центр' if index == 0 else f'Зона {index}',
                         'address': f'Москва, улица Тестовая, {index + 1} к2'}
                        for index, zone in enumerate(ZONE_IDS))
    return AddressService(catalog, path)


async def test_address_buttons_and_existing_session_show_house(tmp_path, app):
    app.addresses = address_service(tmp_path, app.addresses.catalog)
    keyboard = zone_keyboard(app.addresses)
    assert keyboard.inline_keyboard[0][0].text == 'Москва, улица Тестовая, 1 к2'
    assert keyboard.inline_keyboard[0][0].callback_data == 'zone:msk_center'
    chosen = await app.handle(4, 'ул. Тестовая, д. 1, корп. 2')
    assert 'Москва, улица Тестовая, 1 к2' in chosen.text
    assert 'msk_center' not in chosen.text
    assert 'Тестовая, 1 к2' in (await app.handle(4, '/menu')).text
    await app.handle(4, MODES['basket'])
    assert 'Тестовая, 1 к2' in (await app.handle(4, 'молоко 1 л')).text
    assert app.addresses.resolve_address('ул. Тестовая, д. 1, корп. 3')['status'] == 'unsupported'


def test_address_normalization_never_merges_house_suffixes():
    assert address_key('Москва, ул. Тестовая, д. 2/20') == address_key('г. Москва, улица Тестовая, 2/20')
    assert address_key('Тестовая 2/20') != address_key('Тестовая 2 20')
    assert address_key('Тестовая 2г') != address_key('Тестовая 2')
    assert address_key('Тестовая 2д') != address_key('Тестовая 2')


def test_receipt_has_human_names_exact_prices_and_no_catalog_warning(catalog):
    catalog.demo = False
    basket = {'storefront_id': 'ozon_fresh:address:msk_center', 'service': 'ozon_fresh',
              'total': '200', 'complete': True, 'catalog_status': 'collected_with_gaps', 'missing': [],
              'lines': [{'name': 'Молоко Вологжанка', 'sku': '1569065798', 'queries': ['молоко'],
                         'package_qty': '1000', 'package_unit': 'ml', 'packages': 1,
                         'package_price': '91', 'subtotal': '91', 'captured_at': '2026-11-21 04:03:21'},
                        {'name': 'Яйца Ozon fresh', 'sku': '733591168', 'queries': ['яйца'],
                         'package_qty': '10', 'package_unit': 'pcs', 'packages': 1,
                         'package_price': '109', 'subtotal': '109', 'captured_at': '2026-11-21 02:56:32'}]}
    comparison = {'winner': basket['storefront_id'], 'full': [basket], 'partial': []}
    result = BasketAgent(catalog, None).render({'comparison': comparison, 'zone_id': 'msk_center',
                                             'requirements': []}, [], {'catalog_address': 'Москва, Тестовая, 1'})
    assert 'Корзина в Ozon Fresh — 200 ₽' in result.text
    assert '1 уп. по 1 л × 91 ₽ = 91 ₽' in result.text
    assert '1 уп. по 10 шт. × 109 ₽ = 109 ₽' in result.text
    assert '21.11.2026' in result.text and 'Москва, Тестовая, 1' in result.text
    assert all(term not in result.text for term in ('msk_center', 'ozon_fresh:', 'SKU', 'пропуск', 'pcs', 'выгрузка'))
    assert result.structured['comparison'] == comparison


async def test_quantity_clarification_retains_list_and_survives_new_application(app):
    await app.handle(8, 'msk_center')
    await app.handle(8, MODES['basket'])
    first = await app.handle(8, 'Купи мне молоко и яйца, пожалуйста')
    assert first.status == 'need_clarification' and 'для позиции «молоко»' in first.text
    ambiguous = await app.handle(8, '1')
    assert ambiguous.status == 'need_clarification' and 'Уточните единицу' in ambiguous.text
    second = await app.handle(8, 'литр')
    assert second.status == 'need_clarification' and 'для позиции «яйца»' in second.text
    from foodai.application import Application
    restarted = Application(app.storage, app.addresses, app.agents)
    result = await restarted.handle(8, '10 шт.')
    assert result.status == 'ok'
    assert [(r['query'], r['quantity'], r['unit']) for r in result.structured['requirements']] == [
        ('молоко', '1000', 'ml'), ('яйца', '10', 'pcs')]


def test_offline_input_accepts_common_phrasing_but_does_not_invent_quantities():
    items, prompt, pending = prepare_items('Найди литр молока и два килограмма гречки')
    assert not prompt and not pending
    assert [(r['quantity'], r['unit']) for r in items] == [('1000', 'ml'), ('2000', 'g')]
    items, prompt, pending = prepare_items('творог, молоко 1,5 л')
    assert not items and '«творог»' in prompt
    assert 'quantity' not in pending[0] and pending[1]['quantity'] == '1500.0'
