from datetime import datetime, timezone
import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Update
from foodai.application import Application, MODES
from foodai.handlers.bot import make_router, navigation, zone_keyboard
from foodai.storage import Storage


async def test_two_users_and_persistence_and_address_change(app):
    await app.handle(1, '/start')
    await app.handle(1, 'msk_center')
    await app.handle(2, 'msk_09')
    await app.handle(1, MODES['basket'])
    await app.handle(2, MODES['cook'])
    await app.handle(1, 'молоко 1 л, яйца 10 шт')
    assert app.storage.get(1)['mode'] == 'basket'
    assert app.storage.get(2)['mode'] == 'cook'
    assert 'basket_state' not in app.storage.get(2)
    restarted = Application(Storage(app.storage.path), app.addresses, app.agents)
    assert restarted.storage.get(1)['zone_id'] == 'msk_center'
    await restarted.handle(1, 'Изменить адрес')
    assert not restarted.storage.get(1).get('basket_state')
    assert restarted.storage.get(1)['zone_id'] is None
    assert restarted.storage.get(2)['zone_id'] == 'msk_09'


async def test_menu_unknown_address_and_empty_text(app):
    assert (await app.handle(3, 'несуществующий адрес')).status == 'need_clarification'
    await app.handle(3, 'msk_01')
    assert (await app.handle(3, '/menu')).status == 'ok'
    await app.handle(3, MODES['meal_plan'])
    assert 'подключается' in (await app.handle(3, 'Составь рацион')).text
    await app.handle(3, 'Назад')
    assert app.storage.get(3)['mode'] == 'MAIN_MENU'
    await app.handle(3, MODES['basket'])
    assert (await app.handle(3, '')).status == 'need_clarification'


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        return True

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b''


async def test_aiogram_normal_entrypoint_without_telegram_network(app):
    session = RecordingSession()
    bot = Bot('123456789:' + 'A'*35, session=session)
    dp = Dispatcher()
    dp.include_router(make_router(app))
    async def send(index, text):
        update = Update.model_validate({'update_id': index, 'message': {'message_id': index,
            'date': int(datetime.now(timezone.utc).timestamp()),
            'chat': {'id': 5, 'type': 'private'}, 'from': {'id': 5, 'is_bot': False, 'first_name': 'Test'},
            'text': text}})
        await dp.feed_update(bot, update)
    for i, text in enumerate(['/start', 'msk_center', MODES['basket'], 'молоко 1 л, яйца 10 шт'], 1):
        await send(i, text)
    assert any('ТЕСТОВЫЕ' in (getattr(method, 'text', '') or '') for method in session.sent)
    assert len(zone_keyboard().inline_keyboard) == 10
    assert any(button.text == 'Изменить адрес' for row in navigation().keyboard for button in row)
    await bot.session.close()
