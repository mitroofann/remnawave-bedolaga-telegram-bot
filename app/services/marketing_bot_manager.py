"""Marketing bot manager - runs standalone greeting bots in background."""

import asyncio
from typing import Dict

import structlog
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import MarketingBot


logger = structlog.get_logger(__name__)


class MarketingBotInstance:
    """Single marketing bot instance with its own polling loop."""

    def __init__(self, bot_id: int, name: str, token: str, welcome_message: str, image_url: str | None):
        self.bot_id = bot_id
        self.name = name
        self.token = token
        self.welcome_message = welcome_message
        self.image_url = image_url
        self.bot: Bot | None = None
        self.dp: Dispatcher | None = None
        self.task: asyncio.Task | None = None
        self._stopping = False

    async def start(self):
        """Start polling for this bot."""
        if self.task is not None:
            logger.warning('Marketing bot already running', bot_id=self.bot_id, name=self.name)
            return

        try:
            self.bot = Bot(token=self.token)
            self.dp = Dispatcher()

            # Register handler for ANY message
            @self.dp.message()
            async def handle_any_message(message: types.Message):
                """Reply with welcome message + image to any incoming message."""
                try:
                    if self.image_url:
                        await message.answer_photo(
                            photo=self.image_url,
                            caption=self.welcome_message,
                            parse_mode='HTML',
                        )
                    else:
                        await message.answer(
                            text=self.welcome_message,
                            parse_mode='HTML',
                        )
                except Exception as e:
                    logger.error(
                        'Failed to send marketing bot reply',
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
                await self.dp.start_polling(self.bot, allowed_updates=['message'])
            except Exception as e:
                if not self._stopping:
                    logger.error(
                        'Marketing bot polling error, restarting in 5s',
                        bot_id=self.bot_id,
                        name=self.name,
                        error=str(e),
                    )
                    await asyncio.sleep(5)

    async def stop(self):
        """Stop polling for this bot."""
        self._stopping = True

        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

        if self.bot:
            await self.bot.session.close()
            self.bot = None

        self.dp = None
        logger.info('Marketing bot stopped', bot_id=self.bot_id, name=self.name)

    async def reload(self, name: str, token: str, welcome_message: str, image_url: str | None):
        """Reload bot configuration (restart if token changed)."""
        token_changed = self.token != token

        self.name = name
        self.token = token
        self.welcome_message = welcome_message
        self.image_url = image_url

        if token_changed:
            logger.info('Marketing bot token changed, restarting', bot_id=self.bot_id, name=self.name)
            await self.stop()
            await self.start()
        else:
            logger.info('Marketing bot config updated', bot_id=self.bot_id, name=self.name)


class MarketingBotManager:
    """Manages all active marketing bots."""

    def __init__(self):
        self.bots: Dict[int, MarketingBotInstance] = {}
        self._lock = asyncio.Lock()

    async def load_and_start_all(self, db: AsyncSession):
        """Load all active bots from database and start them."""
        async with self._lock:
            result = await db.execute(
                select(MarketingBot).where(MarketingBot.is_active == True)
            )
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
