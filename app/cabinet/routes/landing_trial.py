"""Public landing-funnel TRIAL endpoint (isolated fork feature).

Отдельный роутер под тем же префиксом ``/landing`` с единственным публичным
эндпоинтом ``POST /landing/{slug}/trial``. Вынесен в отдельный файл ради изоляции
фичи (минимум конфликтов при мержах с апстримом). Вся бизнес-логика — в
``app/services/landing_trial_service.py``.
"""

import re

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.ip_utils import get_client_ip
from app.config import settings
from app.database.crud.landing import get_active_landing_by_slug
from app.services.guest_purchase_service import GuestPurchaseError
from app.services.landing_trial_service import (
    claim_free_trial,
    resolve_landing_trial_params,
    start_paid_trial,
)
from app.utils.cache import RateLimitCache, cache


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/landing', tags=['Landing Trial'])

# Валидация контакта — те же правила, что в purchase-флоу (landing.py).
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_TELEGRAM_RE = re.compile(r'^@?[A-Za-z0-9_]{4,32}$')


# ============ Schemas ============


class LandingTrialRequest(BaseModel):
    contact_type: str = Field(pattern=r'^(email|telegram)$')
    contact_value: str = Field(min_length=1, max_length=255)
    # Обязателен ТОЛЬКО если триал платный (requires_payment). Для бесплатного игнорируется.
    payment_method: str | None = Field(default=None, max_length=50, pattern=r'^[a-z0-9_]+$')
    language: str | None = Field(default=None, max_length=5)
    yandex_cid: str | None = Field(default=None, max_length=128, pattern=r'^[A-Za-z0-9._:-]{4,128}$')
    yclid: str | None = Field(default=None, max_length=64, pattern=r'^[0-9]{1,64}$')
    referrer: str | None = Field(default=None, max_length=500)
    subid: str | None = Field(default=None, max_length=255)

    @model_validator(mode='after')
    def validate_contact(self) -> 'LandingTrialRequest':
        value = self.contact_value.strip()
        if self.contact_type == 'email':
            if not _EMAIL_RE.match(value):
                raise ValueError('Invalid email format')
        elif not _TELEGRAM_RE.match(value):
            raise ValueError('Invalid Telegram username format')
        return self


class LandingTrialResponse(BaseModel):
    """Ответ выдачи триала. ``mode`` определяет набор заполненных полей.

    - ``mode='free'`` — триал выдан сразу: ``status``, ``subscription_url`` + креды/автологин.
    - ``mode='paid'`` — нужна оплата: ``purchase_token`` + ``payment_url``
      (дальше фронт ведёт как обычную покупку, статус — через GET /landing/purchase/{token}).
    """

    mode: str  # 'free' | 'paid'
    # free
    status: str | None = None
    subscription_url: str | None = None
    subscription_crypto_link: str | None = None
    contact_type: str | None = None
    cabinet_email: str | None = None
    cabinet_password: str | None = None
    auto_login_token: str | None = None
    recipient_in_bot: bool | None = None
    bot_link: str | None = None
    # paid
    purchase_token: str | None = None
    payment_url: str | None = None


# ============ Route ============


@router.post('/{slug}/trial', response_model=LandingTrialResponse)
async def create_landing_trial(
    body: LandingTrialRequest,
    raw_request: Request,
    slug: str = Path(max_length=100),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Выдать триал через публичную лендинг-воронку (без авторизации).

    Бесплатный триал выдаётся синхронно; платный — заводит покупку+оплату и
    возвращает payment_url (выдача в fulfill_purchase после оплаты).
    """
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'landing_trial', limit=5, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many trial attempts, please try again later',
        )

    landing = await get_active_landing_by_slug(db, slug)
    if landing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Landing page not found')

    contact_value = body.contact_value.strip()

    try:
        params = await resolve_landing_trial_params(db)

        if params.requires_payment and params.price_kopeks > 0:
            # Платный триал — через существующий purchase/платёжный флоу.
            if not body.payment_method:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail='payment_method is required for a paid trial',
                )
            cabinet_base = (settings.CABINET_URL or '').rstrip('/')
            token_placeholder_return = f'{cabinet_base}/buy/success/{{token}}'
            # create_purchase генерит token внутри — сначала сформируем return_url c плейсхолдером,
            # сервис подставит фактический токен через тот же паттерн, что и purchase-флоу.
            purchase_token, payment_url = await start_paid_trial(
                db,
                landing,
                contact_type=body.contact_type,  # type: ignore[arg-type]
                contact_value=contact_value,
                payment_method=body.payment_method,
                return_url=token_placeholder_return,
                subid=body.subid,
                referrer=body.referrer,
            )

            # Кэшируем Yandex CID/yclid/subid по токену — как в purchase-флоу,
            # чтобы fulfill_purchase/конверсии могли их подхватить.
            await _cache_attribution(purchase_token, body)

            return LandingTrialResponse(mode='paid', purchase_token=purchase_token, payment_url=payment_url)

        # Бесплатный триал — синхронная выдача.
        result = await claim_free_trial(
            db,
            landing,
            contact_type=body.contact_type,  # type: ignore[arg-type]
            contact_value=contact_value,
            language=body.language,
        )
        return LandingTrialResponse(
            mode='free',
            status='delivered',
            subscription_url=result.subscription_url,
            subscription_crypto_link=result.subscription_crypto_link,
            contact_type=result.contact_type,
            cabinet_email=result.cabinet_email,
            cabinet_password=result.cabinet_password,
            auto_login_token=result.auto_login_token,
            recipient_in_bot=result.recipient_in_bot,
            bot_link=result.bot_link,
        )
    except GuestPurchaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


async def _cache_attribution(purchase_token: str, body: LandingTrialRequest) -> None:
    """Сохранить CID/yclid/subid в кэш по токену (как в create_landing_purchase)."""
    if body.yandex_cid and settings.YANDEX_OFFLINE_CONV_ENABLED:
        try:
            await cache.set(f'yacid:purchase:{purchase_token}', body.yandex_cid, expire=86400)
        except Exception:
            pass
    if body.yclid and settings.YANDEX_OFFLINE_CONV_ENABLED:
        try:
            await cache.set(f'yclid:purchase:{purchase_token}', body.yclid, expire=86400)
        except Exception:
            pass
    if body.subid:
        try:
            await cache.set(f'subid:purchase:{purchase_token}', body.subid, expire=86400)
        except Exception:
            pass
