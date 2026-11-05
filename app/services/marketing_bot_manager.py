"""Marketing bot manager - runs standalone greeting bots in background."""

import asyncio

import structlog
from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramBadRequest, TelegramUnauthorizedError
from aiogram.types import BufferedInputFile
from aiogram.utils.backoff import BackoffConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import MarketingBot


logger = structlog.get_logger(__name__)

# Картинка по URL скачивается силами серверов Telegram — если им ссылка
# недоступна (закрытый хост, самоподписанный сертификат, не-изображение),
# они отвечают "failed to get HTTP URL content". Тогда бот скачивает картинку
# сам и шлёт её байтами; если и это не удалось — уходит хотя бы текст.
IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 15
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # лимит Telegram для фото

# Маркетинговые боты живут в одном процессе и одном логе с основным ботом, и
# штатный ретрай aiogram (потолок паузы — 5 секунд) превращает отозванный токен
# в поток ошибок: 401 раз в 5 секунд, 238 записей за 10 минут. Поднимаем потолок
# паузы — ошибка остаётся видимой, но не забивает лог.
POLLING_BACKOFF_CONFIG = BackoffConfig(min_delay=1.0, max_delay=60.0, factor=1.5, jitter=0.1)

# Пауза перед перезапуском поллинга, упавшего исключением.
POLLING_RESTART_DELAY_SECONDS = 5

# Сколько ждём штатной остановки поллинга, прежде чем оборвать задачу.
POLLING_STOP_TIMEOUT_SECONDS = 10.0


async def _download_image(image_url: str) -> bytes | None:
    """Скачивает картинку для отправки байтов; None при любой неудаче."""
    import aiohttp

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=IMAGE_DOWNLOAD_TIMEOUT_SECONDS)
        ) as session:
            async with session.get(image_url) as response:
                if response.status != 200:
                    logger.warning(
                        'Marketing bot image download failed: bad status',
                        status=response.status,
                        image_url=image_url,
                    )
                    return None
                data = await response.read()
                if len(data) > MAX_IMAGE_BYTES:
                    logger.warning(
                        'Marketing bot image too large to send',
                        size=len(data),
                        image_url=image_url,
                    )
                    return None
                return data
    except Exception as e:
        logger.warning(
            'Marketing bot image download failed',
            image_url=image_url,
            error=str(e),
        )
        return None


class MarketingBotInstance:
    """Single marketing bot instance with its own polling loop."""

    def __init__(
        self,
        bot_id: int,
        name: str,
        token: str,
        welcome_message: str,
        image_url: str | None,
        button_text: str | None,
        button_url: str | None,
    ):
        self.bot_id = bot_id
        self.name = name
        self.token = token
        self.welcome_message = welcome_message
        self.image_url = image_url
        self.button_text = button_text
        self.button_url = button_url
        self.bot: Bot | None = None
        self.dp: Dispatcher | None = None
        self.task: asyncio.Task | None = None
        self._stopping = False

    async def start(self):
        """Start polling for this bot."""
        # Инстанс переживает остановку и переиспользуется при смене токена
        # и включении бота обратно, поэтому «мёртвая» задача от прошлого
        # запуска не должна считаться работающей.
        if self.task is not None and not self.task.done():
            logger.warning('Marketing bot already running', bot_id=self.bot_id, name=self.name)
            return

        # stop() поднимает _stopping, а _run_polling крутится, пока он опущен.
        # Без сброса флага цикл выходит на первой же итерации: в логе
        # «Marketing bot started», а getUpdates не вызывается ни разу — бот
        # молчит, хотя в кабинете он включён.
        self._stopping = False
        self.task = None

        try:
            self.bot = Bot(token=self.token)
            self.dp = Dispatcher()

            # Register handler for ANY message
            @self.dp.message()
            async def handle_any_message(message: types.Message):
                """Reply with welcome message + image to any incoming message."""
                try:
                    # Prepare reply markup if button is configured
                    reply_markup = None
                    if self.button_text and self.button_url:
                        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                        keyboard = InlineKeyboardMarkup(
                            inline_keyboard=[[InlineKeyboardButton(text=self.button_text, url=self.button_url)]]
                        )
                        reply_markup = keyboard

                    if self.image_url:
                        try:
                            await message.answer_photo(
                                photo=self.image_url,
                                caption=self.welcome_message,
                                parse_mode='HTML',
                                reply_markup=reply_markup,
                            )
                        except TelegramBadRequest as e:
                            # Серверы Telegram не смогли скачать картинку по URL —
                            # качаем сами и шлём байтами; при неудаче остаётся текст.
                            logger.warning(
                                'Marketing bot image URL rejected by Telegram, falling back to upload',
                                bot_id=self.bot_id,
                                name=self.name,
                                image_url=self.image_url,
                                error=str(e),
                            )
                            data = await _download_image(self.image_url)
                            if data is not None:
                                await message.answer_photo(
                                    photo=BufferedInputFile(data, filename='image.jpg'),
                                    caption=self.welcome_message,
                                    parse_mode='HTML',
                                    reply_markup=reply_markup,
                                )
                            else:
                                await message.answer(
                                    text=self.welcome_message,
                                    parse_mode='HTML',
                                    reply_markup=reply_markup,
                                )
                    else:
                        await message.answer(
                            text=self.welcome_message,
                            parse_mode='HTML',
                            reply_markup=reply_markup,
                        )
                except Exception as e:
                    logger.error(
                        'Failed to send marketing bot reply',
                        bot_id=self.bot_id,
                        name=self.name,
                        error=str(e),
                    )

            # На токене мог остаться вебхук — если этого бота раньше настраивали
            # в режиме webhook. С ним getUpdates отвечает 409 Conflict, и поллинг
            # крутится вхолостую, не получая ни одного апдейта. Заодно это первая
            # проверка токена: отозванный Telegram отвергает прямо здесь.
            try:
                await self.bot.delete_webhook(drop_pending_updates=False)
            except TelegramUnauthorizedError as e:
                logger.error(
                    'Marketing bot token rejected by Telegram, polling not started',
                    bot_id=self.bot_id,
                    name=self.name,
                    error=str(e),
                )
                await self._release_bot()
                return
            except Exception as e:
                # Например, api.telegram.org недоступен на старте — поллинг
                # умеет ждать сеть сам, это не повод не запускаться.
                logger.warning(
                    'Marketing bot webhook cleanup failed',
                    bot_id=self.bot_id,
                    name=self.name,
                    error=str(e),
                )

            # Start polling in background task
            self.task = asyncio.create_task(self._run_polling())
            logger.info('Marketing bot started', bot_id=self.bot_id, name=self.name)

        except Exception as e:
            logger.error('Failed to start marketing bot', bot_id=self.bot_id, name=self.name, error=str(e))
            await self.stop()

    async def _run_polling(self):
        """Run polling loop (restarts on errors)."""
        while not self._stopping:
            try:
                await self.dp.start_polling(
                    self.bot,
                    allowed_updates=['message'],
                    # Поллер живёт в том же event loop, что и основной бот, и
                    # по умолчанию start_polling вешает свои обработчики
                    # SIGINT/SIGTERM поверх graceful shutdown приложения —
                    # каждый маркетинговый бот перетирал их своим.
                    handle_signals=False,
                    backoff_config=POLLING_BACKOFF_CONFIG,
                    # Сессию закрывает stop(): с этим флагом start_polling
                    # закрыл бы её сам на выходе из цикла.
                    close_bot_session=False,
                )
            except TelegramUnauthorizedError as e:
                # Токен отозван или перевыпущен: повторять getUpdates
                # бессмысленно, бот будет молчать до правки конфигурации.
                # Выходим из цикла — новый токен придёт через reload().
                logger.error(
                    'Marketing bot token rejected by Telegram, polling stopped until token changes',
                    bot_id=self.bot_id,
                    name=self.name,
                    error=str(e),
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._stopping:
                    logger.error(
                        'Marketing bot polling error, restarting',
                        bot_id=self.bot_id,
                        name=self.name,
                        error=str(e),
                        retry_in=POLLING_RESTART_DELAY_SECONDS,
                    )
                    await asyncio.sleep(POLLING_RESTART_DELAY_SECONDS)

    async def stop(self):
        """Stop polling for this bot."""
        self._stopping = True

        # Гасим поллинг штатно: start_polling держит собственные внутренние
        # задачи (опрос и ожидание стоп-сигнала), и отмена внешней задачи
        # оставляет их жить. Такой «осиротевший» поллер продолжал дёргать
        # Telegram уже отозванным токеном — 401 каждые 5 секунд, пока не
        # перезапустишь контейнер.
        if self.dp is not None:
            try:
                await asyncio.wait_for(self.dp.stop_polling(), timeout=POLLING_STOP_TIMEOUT_SECONDS)
            except RuntimeError:
                # Поллинг ещё не успел стартовать — отмены задачи внизу хватит.
                pass
            except TimeoutError:
                logger.warning(
                    'Marketing bot polling stop timed out, cancelling task',
                    bot_id=self.bot_id,
                    name=self.name,
                )
            except Exception as e:
                logger.warning(
                    'Marketing bot polling stop failed',
                    bot_id=self.bot_id,
                    name=self.name,
                    error=str(e),
                )

        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

        await self._release_bot()
        logger.info('Marketing bot stopped', bot_id=self.bot_id, name=self.name)

    async def _release_bot(self):
        """Закрывает HTTP-сессию бота. Поллинг к этому моменту уже не идёт."""
        if self.bot:
            await self.bot.session.close()
            self.bot = None
        self.dp = None

    async def reload(
        self,
        name: str,
        token: str,
        welcome_message: str,
        image_url: str | None,
        button_text: str | None,
        button_url: str | None,
    ):
        """Reload bot configuration (restart if token changed)."""
        token_changed = self.token != token

        self.name = name
        self.token = token
        self.welcome_message = welcome_message
        self.image_url = image_url
        self.button_text = button_text
        self.button_url = button_url

        if token_changed:
            logger.info('Marketing bot token changed, restarting', bot_id=self.bot_id, name=self.name)
            await self.stop()
            await self.start()
        elif self.task is None or self.task.done():
            # Поллинг уже не идёт — например, на старте Telegram отверг токен
            # или задача умерла. Правка любого поля в кабинете должна поднять
            # бота заново, иначе он останется молчащим до смены токена.
            logger.info('Marketing bot was not running, starting', bot_id=self.bot_id, name=self.name)
            await self.start()
        else:
            logger.info('Marketing bot config updated', bot_id=self.bot_id, name=self.name)


class MarketingBotManager:
    """Manages all active marketing bots."""

    def __init__(self):
        self.bots: dict[int, MarketingBotInstance] = {}
        self._lock = asyncio.Lock()

    async def load_and_start_all(self, db: AsyncSession):
        """Load all active bots from database and start them."""
        async with self._lock:
            result = await db.execute(select(MarketingBot).where(MarketingBot.is_active == True))
            active_bots = result.scalars().all()

            logger.info('Loading marketing bots', count=len(active_bots))

            for bot_record in active_bots:
                await self._start_bot(bot_record)

    async def _start_bot(self, bot_record: MarketingBot):
        """Start a single bot instance."""
        if bot_record.id in self.bots:
            logger.warning('Marketing bot already running', bot_id=bot_record.id)
            return

        instance = MarketingBotInstance(
            bot_id=bot_record.id,
            name=bot_record.name,
            token=bot_record.bot_token,
            welcome_message=bot_record.welcome_message,
            image_url=bot_record.image_url,
            button_text=bot_record.button_text,
            button_url=bot_record.button_url,
        )
        await instance.start()
        self.bots[bot_record.id] = instance

    async def add_bot(self, bot_record: MarketingBot):
        """Add and start a new bot."""
        async with self._lock:
            await self._start_bot(bot_record)

    async def update_bot(self, bot_record: MarketingBot):
        """Update existing bot configuration."""
        async with self._lock:
            if bot_record.id not in self.bots:
                if bot_record.is_active:
                    await self._start_bot(bot_record)
                return

            instance = self.bots[bot_record.id]

            if not bot_record.is_active:
                await instance.stop()
                del self.bots[bot_record.id]
            else:
                await instance.reload(
                    name=bot_record.name,
                    token=bot_record.bot_token,
                    welcome_message=bot_record.welcome_message,
                    image_url=bot_record.image_url,
                    button_text=bot_record.button_text,
                    button_url=bot_record.button_url,
                )

    async def remove_bot(self, bot_id: int):
        """Stop and remove a bot."""
        async with self._lock:
            if bot_id in self.bots:
                await self.bots[bot_id].stop()
                del self.bots[bot_id]

    async def stop_all(self):
        """Stop all running bots."""
        async with self._lock:
            logger.info('Stopping all marketing bots', count=len(self.bots))
            for instance in self.bots.values():
                await instance.stop()
            self.bots.clear()


# Global singleton instance
marketing_bot_manager = MarketingBotManager()
