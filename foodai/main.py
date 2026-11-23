import asyncio
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from foodai.bootstrap import build
from foodai.config import Settings
from foodai.handlers.bot import make_router


async def main():
    settings = Settings.load()
    if not settings.bot_token:
        raise SystemExit('Задайте BOT_TOKEN в .env. Офлайн-демо: python -m foodai.demo')
    application = build(settings)
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(application))
    async with Bot(settings.bot_token) as bot:
        await bot.set_my_commands([BotCommand(command='start', description='Начать'),
                                   BotCommand(command='menu', description='Главное меню'),
                                   BotCommand(command='help', description='Помощь')])
        await dispatcher.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
