"""Authenticated endpoints for the isolated Bulka landing sales flow."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.ip_utils import get_client_ip
from app.database.crud.landing import get_active_landing_by_slug
from app.database.models import User
from app.services.guest_purchase_service import GuestPurchaseError
from app.services.landing_bulka_flow_service import build_bulka_flow_config, create_bulka_purchase
from app.utils.cache import RateLimitCache


router = APIRouter(prefix='/landing', tags=['Bulka Landing Flow'])


class BulkaPurchaseRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    flow_kind: Literal['trial', 'purchase']
    tariff_id: int | None = None
    period_days: int | None = Field(default=None, ge=1)
    payment_method: str = Field(min_length=1, max_length=50, pattern=r'^[a-z0-9_]+$')
    payment_sub_option: str | None = Field(default=None, min_length=1, max_length=50, pattern=r'^[a-z0-9_]+$')
    language: str | None = Field(default=None, max_length=5)
    yandex_cid: str | None = Field(default=None, max_length=128, pattern=r'^[A-Za-z0-9._:-]{4,128}$')
    referrer: str | None = Field(default=None, max_length=500)
    subid: str | None = Field(default=None, max_length=255)

    @model_validator(mode='after')
    def validate_selection(self) -> 'BulkaPurchaseRequest':
        if self.flow_kind == 'purchase' and (self.tariff_id is None or self.period_days is None):
            raise ValueError('tariff_id and period_days are required for purchase')
        if self.flow_kind == 'trial' and (self.tariff_id is not None or self.period_days is not None):
            raise ValueError('tariff_id and period_days are forbidden for trial')
        return self


class BulkaPurchaseResponse(BaseModel):
    purchase_token: str
    payment_url: str
    flow_kind: Literal['trial', 'purchase']
    landing_slug: str
    landing_template: Literal['bulka_sales_flow']


def _raise(error: GuestPurchaseError) -> None:
    detail = {'code': getattr(error, 'code', 'bulka_flow_error'), 'message': error.message}
    raise HTTPException(status_code=error.status_code, detail=detail) from error


@router.get('/{slug}/bulka-flow')
async def get_bulka_flow(
    raw_request: Request,
    slug: str = Path(max_length=100),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Return the server-authoritative authenticated Bulka checkout configuration."""
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'bulka_flow_config', limit=60, window=60, fail_closed=True):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail='Too many requests')
    try:
        landing = await get_active_landing_by_slug(db, slug)
        return await build_bulka_flow_config(db, landing, user)
    except GuestPurchaseError as exc:
        _raise(exc)


@router.post('/{slug}/bulka-flow/purchase', response_model=BulkaPurchaseResponse)
async def create_bulka_flow_purchase(
    body: BulkaPurchaseRequest,
    raw_request: Request,
    slug: str = Path(max_length=100),
    idempotency_key: UUID = Header(alias='Idempotency-Key'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create exactly one provider payment per user/landing/idempotency key."""
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'bulka_flow_purchase', limit=30, window=60, fail_closed=True):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail='Too many purchase attempts, please try again later')
    try:
        landing = await get_active_landing_by_slug(db, slug)
        result = await create_bulka_purchase(
            db,
            landing=landing,
            user=user,
            flow_kind=body.flow_kind,
            tariff_id=body.tariff_id,
            period_days=body.period_days,
            payment_method=body.payment_method,
            payment_sub_option=body.payment_sub_option,
            idempotency_key=str(idempotency_key),
            language=body.language,
            yandex_cid=body.yandex_cid,
            referrer=body.referrer,
            subid=body.subid,
        )
    except GuestPurchaseError as exc:
        _raise(exc)
    return BulkaPurchaseResponse(
        purchase_token=result.purchase.token,
        payment_url=result.payment_url,
        flow_kind=result.purchase.flow_kind,
        landing_slug=result.purchase.landing_slug or slug,
        landing_template='bulka_sales_flow',
    )
