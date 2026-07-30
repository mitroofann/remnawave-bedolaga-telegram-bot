"""Бот-заглушка для СТАРОГО Telegram-бота после переезда Stable VPN → Bulka VPN.

Работает на СТАРОМ токене. На любое сообщение/кнопку отвечает картинкой нового
бренда, коротким текстом о переезде и кнопкой на нового бота. Дополнительно —
админ-команда /broadcast для разовой рассылки того же сообщения по всей базе.

Отдельный лёгкий процесс (polling): не тянет БД-модели/сервисы основного проекта,
не выполняет никакой бизнес-логики. Живёт как угодно долго на копейках ресурсов.

Запуск: см. README.md. Токен — СТАРОГО бота. НЕ запускать, пока крутится старый
основной бот на том же токене (один токен — один poller).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)


logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('stub_bot')


# ============ Конфигурация (через переменные окружения) ============

BOT_TOKEN = os.environ['BOT_TOKEN']  # СТАРЫЙ токен

# Username нового бота БЕЗ @ (напр. "bulkavpn_bot"). Впиши своё.
BULKA_BOT_USERNAME = os.environ.get('BULKA_BOT_USERNAME', 'vpnbulka_bot')

# Путь к картинке нового бренда (лого Bulka VPN), лежит рядом с ботом.
LOGO_PATH = os.environ.get('BULKA_LOGO_PATH', 'assets/bulka_logo.png')

# ID админов через запятую (для /broadcast). Напр. "216332351,161893461".
ADMIN_IDS = {int(x) for x in os.environ.get('ADMIN_IDS', '').replace(' ', '').split(',') if x.strip().isdigit()}

# Строка подключения к ТОЙ ЖЕ БД (нужна только для /broadcast).
# Формат asyncpg: postgresql://user:pass@host:5432/dbname
# (если в основном .env стоит postgresql+asyncpg://... — убери "+asyncpg" для прямого asyncpg).
DATABASE_URL = os.environ.get('DATABASE_URL', '')

# Троттлинг рассылки: сообщений в секунду (Telegram лимит ~30, берём с запасом).
BROADCAST_RATE = float(os.environ.get('BROADCAST_RATE', '20'))


# ============ Текст сообщения (согласован — правится здесь) ============

STUB_CAPTION = (
    '🚀 <b>Мы переехали!</b>\n\n'
    'Stable VPN сменил имя — теперь мы <b>Bulka VPN</b> 🥐\n\n'
    'Ваша подписка, баланс и все настройки уже перенесены в новый бот. '
    'Просто откройте его и войдите — всё на месте.\n\n'
    '👇 Нажмите кнопку ниже'
)


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text='🥐 Открыть Bulka VPN', url=f'https://t.me/{BULKA_BOT_USERNAME}')]]
    )


def create_bot() -> Bot:
    """Bot по образцу app/bot_factory.py основного проекта."""
    return Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


dp = Dispatcher()


# ============ Хэндлеры ============


@dp.message(Command('broadcast'))
async def cmd_broadcast(message: Message, bot: Bot) -> None:
    """Разовая рассылка сообщения-заглушки по всей базе. Только для админов."""
    if message.from_user is None or message.from_user.id not in ADMIN_IDS:
        # Не-админ, приславший /broadcast, получает обычную заглушку.
        await _send_stub(message)
        return

    if not DATABASE_URL:
        await message.answer('❌ DATABASE_URL не задан — рассылка недоступна.')
        return

    await message.answer('📤 Начинаю рассылку по базе…')
    try:
        sent, blocked, failed = await _run_broadcast(bot)
    except Exception as error:
        logger.exception('Broadcast failed')
        await message.answer(f'❌ Рассылка прервана ошибкой: {error}')
        return

    await message.answer(
        f'✅ Рассылка завершена.\nОтправлено: {sent}\nЗаблокировали бота: {blocked}\nПрочие ошибки: {failed}'
    )


@dp.message()
async def any_message(message: Message) -> None:
    """Любое сообщение → заглушка о переезде."""
    await _send_stub(message)


@dp.callback_query()
async def any_callback(callback: CallbackQuery) -> None:
    """Нажатия на старые инлайн-кнопки (из старых сообщений) → тоже заглушка."""
    try:
        await callback.answer()
    except Exception:
        pass
    if callback.message is not None:
        await _send_stub(callback.message)


# ============ Отправка заглушки ============


async def _send_stub(message: Message) -> None:
    """Шлёт фото нового бренда + текст + кнопку. Если файла лого нет — только текст."""
    kb = _keyboard()
    logo = Path(LOGO_PATH)
    try:
        if logo.is_file():
            await message.answer_photo(FSInputFile(str(logo)), caption=STUB_CAPTION, reply_markup=kb)
        else:
            logger.warning('Лого не найдено по пути %s — шлю без картинки', LOGO_PATH)
            await message.answer(STUB_CAPTION, reply_markup=kb)
    except TelegramForbiddenError:
        pass  # юзер заблокировал бота — молча пропускаем
    except Exception:
        logger.exception('Не удалось отправить заглушку')


# ============ Рассылка по базе ============


async def _fetch_all_telegram_ids() -> list[int]:
    """Все telegram_id живых пользователей из общей БД (прямой asyncpg, без моделей проекта)."""
    import asyncpg  # noqa: PLC0415

    dsn = DATABASE_URL.replace('+asyncpg', '')  # asyncpg не понимает SQLAlchemy-схему
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT telegram_id FROM users WHERE telegram_id IS NOT NULL AND (status IS NULL OR status <> 'deleted')"
        )
    finally:
        await conn.close()
    return [r['telegram_id'] for r in rows if r['telegram_id']]


async def _run_broadcast(bot: Bot) -> tuple[int, int, int]:
    """Шлёт STUB_CAPTION+лого всем. Возвращает (sent, blocked, failed)."""
    ids = await _fetch_all_telegram_ids()
    logger.info('Broadcast: %d получателей', len(ids))

    kb = _keyboard()
    logo = Path(LOGO_PATH)
    use_photo = logo.is_file()
    delay = 1.0 / BROADCAST_RATE if BROADCAST_RATE > 0 else 0.0

    sent = blocked = failed = 0
    for tg_id in ids:
        try:
            if use_photo:
                await bot.send_photo(tg_id, FSInputFile(str(logo)), caption=STUB_CAPTION, reply_markup=kb)
            else:
                await bot.send_message(tg_id, STUB_CAPTION, reply_markup=kb)
            sent += 1
        except TelegramRetryAfter as e:
            # Telegram просит подождать — ждём и повторяем этого получателя.
            await asyncio.sleep(e.retry_after + 1)
            try:
                if use_photo:
                    await bot.send_photo(tg_id, FSInputFile(str(logo)), caption=STUB_CAPTION, reply_markup=kb)
                else:
                    await bot.send_message(tg_id, STUB_CAPTION, reply_markup=kb)
                sent += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            blocked += 1  # юзер заблокировал бота
        except Exception:
            failed += 1
        await asyncio.sleep(delay)

        if (sent + blocked + failed) % 500 == 0:
            logger.info('Broadcast прогресс: sent=%d blocked=%d failed=%d', sent, blocked, failed)

    return sent, blocked, failed


# ============ Запуск ============


async def main() -> None:
    bot = create_bot()
    # Снимаем возможный webhook старого бота, иначе polling конфликтует.
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info('Stub-бот запущен (polling). Новый бот: @%s', BULKA_BOT_USERNAME)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
