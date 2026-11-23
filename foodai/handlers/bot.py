from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup
from foodai.application import MODES
from foodai.services.address import ZONE_NAMES
from foodai.services.catalog import ZONE_IDS


def navigation():
    rows = [[KeyboardButton(text=label)] for label in MODES.values()]
    rows.append([KeyboardButton(text='Назад'), KeyboardButton(text='Главное меню')])
    rows.append([KeyboardButton(text='Изменить адрес'),
                 KeyboardButton(text='Отправить геопозицию', request_location=True)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def zone_keyboard(addresses=None):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=addresses.zone_label(zid) if addresses else name, callback_data='zone:' + zid)]
        for zid, name in zip(ZONE_IDS, ZONE_NAMES)])


def make_router(application):
    router = Router(name='foodai')

    async def deliver(message, user_id, text='', location=None):
        result = await application.handle(user_id, text, location)
        # Plain text avoids interpreting model and catalog strings as Telegram HTML.
        for start in range(0, len(result.text), 3800):
            await message.answer(result.text[start:start + 3800], reply_markup=navigation())
        if not application.storage.get(user_id).get('zone_id'):
            await message.answer('Выберите адрес дома для расчёта корзины', reply_markup=zone_keyboard(application.addresses))

    @router.callback_query(F.data.startswith('zone:'))
    async def choose_zone(callback: CallbackQuery):
        await callback.answer()
        if callback.message and callback.message.chat.type == 'private':
            # Selection is accepted only during address selection to invalidate old baskets.
            if application.storage.get(callback.from_user.id).get('zone_id'):
                await callback.message.answer('Для смены зоны нажмите «Изменить адрес».')
                return
            await deliver(callback.message, callback.from_user.id, callback.data.removeprefix('zone:'))

    @router.message(F.chat.type == 'private')
    async def message_received(message: Message):
        if not message.from_user:
            return
        text = message.text or ''
        if text.startswith('/'):
            text = text.split()[0].split('@')[0]
        location = {'lat': message.location.latitude, 'lon': message.location.longitude} if message.location else None
        await deliver(message, message.from_user.id, text, location)

    return router
