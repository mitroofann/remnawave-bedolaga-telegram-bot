"""Жизненный цикл поллинга маркетингового бота.

Боты из ``app/services/marketing_bot_manager.py`` переживают остановку и
переиспользуются при перезапуске (смена токена, выключение и включение бота
в кабинете). Здесь закреплены два места, из-за которых бот «Ping VPN» молча
не реагировал:

1. ``stop()`` поднимает флаг ``_stopping``, а ``start()`` его не сбрасывал —
   цикл опроса выходил на первой итерации, хотя в лог писалось
   «Marketing bot started».
2. Остановка поллинга отменой внешней задачи не убивает внутренний цикл
   aiogram: осиротевший поллер продолжал дёргать Telegram уже отозванным
   токеном — 401 каждые 5 секунд до перезапуска контейнера.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramUnauthorizedError

from app.services import marketing_bot_manager as mbm


TOKEN = '7782954518:AAEtest-token-for-unit-tests'


class _FakeSession(BaseSession):
    """Сессия без сети: пишет имена вызовов и, если попросят, отвечает 401."""

    def __init__(self, calls: list[str], *, unauthorized: bool = False) -> None:
        super().__init__()
        self.calls = calls
        self.unauthorized = unauthorized

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(type(method).__name__)
        if self.unauthorized:
            raise TelegramUnauthorizedError(method=method, message='Unauthorized')
        return True

    async def stream_content(self, *args: Any, **kwargs: Any):
        yield b''

    async def close(self) -> None:
        return


class _RecordingDispatcher:
    """Диспетчер, который запоминает старт/остановку поллинга."""

    def __init__(
        self,
        *,
        starts: list[int],
        stops: list[bool],
        stop_raises: type[Exception] | None = None,
    ) -> None:
        self.starts = starts
        self.stops = stops
        self.stop_raises = stop_raises

    def message(self, *args: Any, **kwargs: Any):
        def _register(func):
            return func

        return _register

    async def start_polling(self, *bots: Any, **kwargs: Any) -> None:
        self.starts.append(1)
        await asyncio.Event().wait()  # поллинг живёт до явной остановки

    async def stop_polling(self) -> None:
        self.stops.append(True)
        if self.stop_raises is not None:
            msg = 'Polling is not started'
            raise self.stop_raises(msg)


@pytest.fixture
def fake_bot(monkeypatch: pytest.MonkeyPatch):
    """Подменяет Bot: сетевые вызовы идут в _FakeSession."""
    calls: list[str] = []
    created: list[Bot] = []

    def _make(token: str, *, unauthorized: bool = False) -> Bot:
        bot = Bot(token=token)
        bot.session = _FakeSession(calls, unauthorized=unauthorized)
        created.append(bot)
        return bot

    monkeypatch.setattr(mbm, 'Bot', _make)
    return _make, calls, created


def _install_dispatcher(monkeypatch: pytest.MonkeyPatch, dispatcher: _RecordingDispatcher) -> None:
    monkeypatch.setattr(mbm, 'Dispatcher', lambda: dispatcher)


def _make_instance() -> mbm.MarketingBotInstance:
    return mbm.MarketingBotInstance(
        bot_id=5,
        name='Ping VPN',
        token=TOKEN,
        welcome_message='Привет',
        image_url=None,
        button_text=None,
        button_url=None,
    )


@pytest.mark.asyncio
async def test_start_after_stop_polls_again(monkeypatch: pytest.MonkeyPatch, fake_bot) -> None:
    """Включение бота обратно снова поднимает опрос Telegram.

    Регрессия: ``stop()`` выставляет ``_stopping = True`` и оставляет флаг
    поднятым, поэтому после ``start()`` цикл ``_run_polling`` не делал ни одной
    итерации — бот не отвечал даже с живым токеном.
    """
    starts: list[int] = []
    stops: list[bool] = []
    _install_dispatcher(monkeypatch, _RecordingDispatcher(starts=starts, stops=stops))

    instance = _make_instance()
    await instance.start()
    await asyncio.sleep(0)
    assert starts == [1], 'первый start() не начал опрос'

    await instance.stop()
    await instance.start()
    await asyncio.sleep(0)
    assert starts == [1, 1], 'после stop() и start() опрос не возобновился'

    await instance.stop()


@pytest.mark.asyncio
async def test_stop_asks_dispatcher_to_stop_polling(monkeypatch: pytest.MonkeyPatch, fake_bot) -> None:
    """stop() гасит поллинг через диспетчер, а не только отменой задачи.

    Отмена внешней задачи не трогает внутренние задачи ``start_polling``:
    такой осиротевший поллер продолжал крутиться с отозванным токеном.
    """
    starts: list[int] = []
    stops: list[bool] = []
    _install_dispatcher(monkeypatch, _RecordingDispatcher(starts=starts, stops=stops))

    instance = _make_instance()
    await instance.start()
    await asyncio.sleep(0)
    await instance.stop()

    assert stops, 'stop() не попросил диспетчер остановить поллинг'
    assert instance.task is None


@pytest.mark.asyncio
async def test_stop_survives_dispatcher_without_running_polling(
    monkeypatch: pytest.MonkeyPatch,
    fake_bot,
) -> None:
    """Поллинг не успел стартовать — stop() не падает на RuntimeError."""
    starts: list[int] = []
    stops: list[bool] = []
    _install_dispatcher(
        monkeypatch,
        _RecordingDispatcher(starts=starts, stops=stops, stop_raises=RuntimeError),
    )

    instance = _make_instance()
    await instance.start()
    await asyncio.sleep(0)
    await instance.stop()

    assert instance.task is None


@pytest.mark.asyncio
async def test_start_drops_stale_webhook(monkeypatch: pytest.MonkeyPatch, fake_bot) -> None:
    """Перед опросом снимается вебхук: иначе getUpdates отвечает 409 Conflict."""
    _, calls, _ = fake_bot
    starts: list[int] = []
    stops: list[bool] = []
    _install_dispatcher(monkeypatch, _RecordingDispatcher(starts=starts, stops=stops))

    instance = _make_instance()
    await instance.start()
    await asyncio.sleep(0)

    assert 'DeleteWebhook' in calls, 'вебхук не снят — поллинг упрётся в Conflict'
    await instance.stop()


@pytest.mark.asyncio
async def test_start_does_not_poll_with_rejected_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Отозванный токен не запускает поллинг, который вечно ловит 401."""
    calls: list[str] = []

    def _make_rejected(token: str, **_kwargs: Any) -> Bot:
        bot = Bot(token=token)
        bot.session = _FakeSession(calls, unauthorized=True)
        return bot

    monkeypatch.setattr(mbm, 'Bot', _make_rejected)
    starts: list[int] = []
    stops: list[bool] = []
    _install_dispatcher(monkeypatch, _RecordingDispatcher(starts=starts, stops=stops))

    instance = _make_instance()
    await instance.start()
    await asyncio.sleep(0)

    assert starts == [], 'поллинг запущен, хотя токен отклонён Telegram'
    assert instance.task is None
