"""Landing-funnel trial feature (isolated fork feature).

Позволяет выдавать ТРИАЛ (пробную подписку) через публичную лендинг-воронку
без авторизации — бесплатный (выдаётся сразу) или платный (через существующий
guest-purchase платёжный флоу, помечается ``GuestPurchase.is_trial``).

Изоляция: вся бизнес-логика фичи собрана здесь. В существующие модули — только
тонкие врезки (регистрация роутера, ветка в ``fulfill_purchase``, опц. поля схем).
Бизнес-логика триала и создания пользователя НЕ дублируется — переиспользуются:
- параметры триала: ``get_trial_tariff`` + ``settings.TRIAL_*`` (как в кабинете/боте);
- гейт «один триал»: ``User.is_trial_already_used()`` + ``settings.is_trial_disabled_for_user``;
- создание юзера + пароль + реф-код: ``guest_purchase_service._find_or_create_user``;
- выдача подписки: ``create_trial_subscription``;
- провижининг панели: ``SubscriptionService.create_remnawave_user``;
- автологин: ``create_auto_login_token``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.auth.jwt_handler import create_auto_login_token
from app.config import settings
from app.database.crud.subscription import create_trial_subscription
from app.database.crud.tariff import get_tariff_by_id, get_trial_tariff
from app.database.models import GuestPurchase, GuestPurchaseStatus, LandingPage, User
from app.services.guest_purchase_service import (
    GuestPurchaseError,
    _find_or_create_user,
    create_purchase,
)
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)

# Тайм-баунд провижининга панели (как REMNAWAVE_SYNC_TIMEOUT в кабинетном триале):
# на таймаут подписка уже создана, ставим в remnawave_retry_queue, а не держим ответ.
_REMNAWAVE_SYNC_TIMEOUT = 10.0


@dataclass(slots=True)
class TrialParams:
    """Разрешённые параметры триала (единый источник правды — тариф + settings)."""

    duration_days: int
    traffic_limit_gb: int
    device_limit: int
    requires_payment: bool
    price_kopeks: int
    tariff_id: int | None
    squads: list[str]


def is_landing_trial_globally_enabled() -> bool:
    """Глобальный kill-switch фичи (env LANDING_TRIAL_ENABLED)."""
    return bool(getattr(settings, 'LANDING_TRIAL_ENABLED', False))


async def resolve_landing_trial_params(db: AsyncSession) -> TrialParams:
    """Единый резолвер параметров триала.

    Повторяет логику кабинета/бота: базовые значения из ``settings.TRIAL_*``,
    перекрытие триал-тарифом (``get_trial_tariff`` → fallback ``TRIAL_TARIFF_ID``).
    Триал-тариф намеренно может быть неактивным — по ``is_active`` не отбраковываем.
    """
    duration_days = settings.TRIAL_DURATION_DAYS
    traffic_limit_gb = settings.TRIAL_TRAFFIC_LIMIT_GB
    device_limit = settings.TRIAL_DEVICE_LIMIT
    requires_payment = bool(settings.TRIAL_PAYMENT_ENABLED)
    price_kopeks = settings.TRIAL_ACTIVATION_PRICE if requires_payment else 0
    tariff_id: int | None = None
    squads: list[str] = []

    try:
        trial_tariff = await get_trial_tariff(db)
        if not trial_tariff:
            trial_tariff_id = settings.get_trial_tariff_id()
            if trial_tariff_id > 0:
                trial_tariff = await get_tariff_by_id(db, trial_tariff_id)

        if trial_tariff:
            from app.database.crud.server_squad import get_effective_tariff_squad_uuids

            traffic_limit_gb = trial_tariff.traffic_limit_gb
            device_limit = trial_tariff.device_limit
            tariff_id = trial_tariff.id
            squads = await get_effective_tariff_squad_uuids(db, trial_tariff.allowed_squads)
            tariff_trial_days = getattr(trial_tariff, 'trial_duration_days', None)
            if tariff_trial_days:
                duration_days = tariff_trial_days
    except Exception as error:
        logger.error('resolve_landing_trial_params failed, using settings defaults', error=str(error))

    if not squads:
        try:
            from app.database.crud.server_squad import get_random_trial_squad_uuid

            trial_squad_uuid = await get_random_trial_squad_uuid(db)
            if trial_squad_uuid:
                squads = [trial_squad_uuid]
        except Exception as error:
            logger.error('trial squad fallback failed', error=str(error))

    return TrialParams(
        duration_days=duration_days,
        traffic_limit_gb=traffic_limit_gb,
        device_limit=device_limit,
        requires_payment=requires_payment,
        price_kopeks=price_kopeks,
        tariff_id=tariff_id,
        squads=squads,
    )


async def get_landing_trial_info(db: AsyncSession, landing: LandingPage) -> dict | None:
    """Блок ``trial`` для публичного конфига лендинга. ``None`` если фича недоступна."""
    if not is_landing_trial_globally_enabled():
        return None
    if not getattr(landing, 'trial_enabled', False):
        return None

    params = await resolve_landing_trial_params(db)
    return {
        'enabled': True,
        'duration_days': params.duration_days,
        'traffic_limit_gb': params.traffic_limit_gb,
        'device_limit': params.device_limit,
        'requires_payment': params.requires_payment,
        'price_kopeks': params.price_kopeks,
        'price_rubles': round(params.price_kopeks / 100, 2),
    }


def _assert_landing_trial_enabled(landing: LandingPage) -> None:
    """Гейт доступности фичи (env + флаг лендинга). Бросает GuestPurchaseError 403."""
    if not is_landing_trial_globally_enabled() or not getattr(landing, 'trial_enabled', False):
        raise GuestPurchaseError('Trial is not enabled for this landing', status_code=403)


def _assert_contact_allowed(contact_type: str) -> None:
    """Гейт по типу аккаунта (TRIAL_DISABLED_FOR). Тот же, что в кабинете/боте."""
    auth_type = 'email' if contact_type == 'email' else 'telegram'
    if settings.is_trial_disabled_for_user(auth_type):
        raise GuestPurchaseError('Trial is not available for this account type', status_code=400)


async def _find_existing_user_by_contact(
    db: AsyncSession,
    contact_type: str,
    contact_value: str,
) -> User | None:
    """Найти УЖЕ существующего пользователя по контакту (без создания).

    Нужно для гейта «один триал на контакт» ДО создания юзера/подписки.
    """
    if contact_type == 'email':
        result = await db.execute(select(User).where(User.email == contact_value))
        return result.scalars().first()

    username = contact_value.lstrip('@').lower()
    result = await db.execute(select(User).where(func.lower(User.username) == username))
    return result.scalars().first()


async def _assert_trial_not_used(db: AsyncSession, contact_type: str, contact_value: str) -> None:
    """Гейт «один триал на контакт». Бросает GuestPurchaseError 409, если уже использован."""
    existing = await _find_existing_user_by_contact(db, contact_type, contact_value)
    if existing is None:
        return
    # Подгружаем подписки для is_trial_already_used()
    await db.refresh(existing, ['subscriptions'])
    if existing.is_trial_already_used():
        raise GuestPurchaseError('Trial already used', status_code=409)


@dataclass(slots=True)
class FreeTrialResult:
    """Результат синхронной выдачи бесплатного триала."""

    subscription_url: str | None
    subscription_crypto_link: str | None
    contact_type: str
    cabinet_email: str | None
    cabinet_password: str | None
    auto_login_token: str | None
    recipient_in_bot: bool | None
    bot_link: str | None


async def _provision_trial_subscription(db: AsyncSession, user_id: int, params: TrialParams):
    """Создать trial-подписку и провижинить в панели. Возвращает Subscription."""
    subscription = await create_trial_subscription(
        db=db,
        user_id=user_id,
        duration_days=params.duration_days,
        traffic_limit_gb=params.traffic_limit_gb,
        device_limit=params.device_limit,
        connected_squads=params.squads or None,
        tariff_id=params.tariff_id,
    )

    subscription_service = SubscriptionService()
    panel_user = None
    try:
        if subscription_service.is_configured:
            async with asyncio.timeout(_REMNAWAVE_SYNC_TIMEOUT):
                panel_user = await subscription_service.create_remnawave_user(db, subscription)
                await db.refresh(subscription)
    except Exception as error:
        logger.error('Failed to create RemnaWave user for landing trial', error=str(error))

    # create_remnawave_user глотает RemnaWaveAPIError и возвращает None (не бросает) —
    # поэтому проверяем результат явно и ставим в ретрай (как в кабинетном триале).
    if subscription_service.is_configured and panel_user is None:
        try:
            from app.services.remnawave_retry_queue import remnawave_retry_queue

            remnawave_retry_queue.enqueue(
                subscription_id=subscription.id,
                user_id=user_id,
                action='create',
            )
            logger.warning(
                'Landing trial RemnaWave user not provisioned, enqueued for retry',
                subscription_id=subscription.id,
                user_id=user_id,
            )
        except Exception:
            logger.exception('Failed to enqueue remnawave retry for landing trial')

    return subscription


async def claim_free_trial(
    db: AsyncSession,
    landing: LandingPage,
    *,
    contact_type: Literal['email', 'telegram'],
    contact_value: str,
    language: str | None = None,
) -> FreeTrialResult:
    """Синхронно выдать БЕСПЛАТНЫЙ триал через лендинг-воронку.

    Гейты → создание/поиск юзера (+пароль для email) → trial-подписка →
    провижининг панели → автологин. НЕ коммитит внутри дочерних функций —
    коммитим здесь один раз.
    """
    _assert_landing_trial_enabled(landing)
    _assert_contact_allowed(contact_type)
    await _assert_trial_not_used(db, contact_type, contact_value)

    params = await resolve_landing_trial_params(db)

    # Создаём/находим юзера. purchase=None → для email пароль сгенерится, но plaintext
    # потеряется; поэтому для email генерируем креды сами через временный holder.
    class _CredHolder:
        cabinet_password: str | None = None

    holder = _CredHolder()
    user, is_new_account = await _find_or_create_user(
        db,
        contact_type,
        contact_value,
        purchase=holder,  # type: ignore[arg-type]  # duck-typed: нужен только .cabinet_password
        tariff_id=params.tariff_id,
    )
    if language and not getattr(user, 'language', None):
        user.language = language

    subscription = await _provision_trial_subscription(db, user.id, params)

    cabinet_email = user.email if contact_type == 'email' else None
    cabinet_password = holder.cabinet_password if contact_type == 'email' else None
    auto_login = None
    if contact_type == 'email' and is_new_account:
        auto_login = create_auto_login_token(user.id)

    recipient_in_bot: bool | None = None
    bot_link: str | None = None
    if contact_type == 'telegram':
        recipient_in_bot = user.telegram_id is not None
        if not recipient_in_bot:
            bot_username = settings.get_bot_username()
            if bot_username:
                bot_link = f'https://t.me/{bot_username}'

    await db.commit()
    await db.refresh(subscription)

    logger.info(
        'Landing free trial claimed',
        landing_slug=landing.slug,
        user_id=user.id,
        contact_type=contact_type,
        is_new_account=is_new_account,
    )

    return FreeTrialResult(
        subscription_url=subscription.subscription_url,
        subscription_crypto_link=subscription.subscription_crypto_link,
        contact_type=contact_type,
        cabinet_email=cabinet_email,
        cabinet_password=cabinet_password,
        auto_login_token=auto_login,
        recipient_in_bot=recipient_in_bot,
        bot_link=bot_link,
    )


async def start_paid_trial(
    db: AsyncSession,
    landing: LandingPage,
    *,
    contact_type: Literal['email', 'telegram'],
    contact_value: str,
    payment_method: str,
    return_url: str,
    subid: str | None = None,
    referrer: str | None = None,
) -> tuple[str, str]:
    """Начать ПЛАТНЫЙ триал через существующий guest-purchase платёжный флоу.

    Создаёт ``GuestPurchase(is_trial=True)`` с ценой/тарифом/длительностью триала и
    платёж провайдера. Возвращает ``(purchase_token, payment_url)``. Выдача подписки
    произойдёт в ``fulfill_purchase`` (ветка ``is_trial`` → ``provision_trial_for_purchase``).
    """
    _assert_landing_trial_enabled(landing)
    _assert_contact_allowed(contact_type)
    await _assert_trial_not_used(db, contact_type, contact_value)

    params = await resolve_landing_trial_params(db)
    if not params.requires_payment or params.price_kopeks <= 0:
        raise GuestPurchaseError('Trial is free — use the free trial flow', status_code=400)
    if params.tariff_id is None:
        raise GuestPurchaseError('Paid trial requires a configured trial tariff', status_code=500)

    tariff = await get_tariff_by_id(db, params.tariff_id)
    if tariff is None:
        raise GuestPurchaseError('Trial tariff not found', status_code=500)

    purchase = await create_purchase(
        db,
        landing=landing,
        tariff=tariff,
        period_days=params.duration_days,
        amount_kopeks=params.price_kopeks,
        contact_type=contact_type,
        contact_value=contact_value,
        payment_method=payment_method,
        subid=subid,
        referrer=referrer,
        commit=False,
    )
    purchase.is_trial = True

    # Подставляем фактический токен в return_url (как в create_landing_purchase):
    # вызывающий передаёт шаблон с плейсхолдером {token}.
    resolved_return_url = return_url.replace('{token}', purchase.token)

    from app.services.payment_service import PaymentService

    payment_service = PaymentService()
    payment_result = await payment_service.create_guest_payment(
        db=db,
        amount_kopeks=params.price_kopeks,
        payment_method=payment_method,
        description=f'Trial {tariff.name} — {params.duration_days}d',
        purchase_token=purchase.token,
        return_url=resolved_return_url,
    )
    if payment_result is None:
        await db.rollback()
        raise GuestPurchaseError('Payment provider is unavailable', status_code=502)

    payment_url = payment_result.get('payment_url')
    if not payment_url:
        await db.rollback()
        raise GuestPurchaseError('Payment provider returned an invalid response', status_code=502)

    await db.commit()
    await db.refresh(purchase)

    logger.info(
        'Landing paid trial started',
        landing_slug=landing.slug,
        purchase_token=purchase.token[:5],
        amount_kopeks=params.price_kopeks,
    )

    return purchase.token, payment_url


async def provision_trial_for_purchase(
    db: AsyncSession,
    purchase: GuestPurchase,
    user: User,
) -> None:
    """Выдать trial-подписку по оплаченной ``GuestPurchase(is_trial=True)``.

    Вызывается из ``fulfill_purchase`` (единственная врезка). Держит логику платного
    триала здесь, в изолированном модуле. Покупка уже под FOR UPDATE в вызывающем коде;
    коммит делает вызывающий ``fulfill_purchase``.
    """
    params = await resolve_landing_trial_params(db)
    subscription = await _provision_trial_subscription(db, user.id, params)

    purchase.subscription_url = subscription.subscription_url
    purchase.subscription_crypto_link = subscription.subscription_crypto_link
    purchase.status = GuestPurchaseStatus.DELIVERED.value
    purchase.user_id = user.id

    from datetime import UTC, datetime

    purchase.delivered_at = datetime.now(UTC)

    logger.info(
        'Landing paid trial fulfilled',
        purchase_id=purchase.id,
        user_id=user.id,
    )
