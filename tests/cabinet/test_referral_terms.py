"""Contract tests for effective first-payment referral commission terms."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import referral as route
from app.config import settings


def _legacy_values(*, commission_percent: int, first_payment_commission_percent: int | None):
    return SimpleNamespace(
        commission_percent=commission_percent,
        first_payment_commission_percent=first_payment_commission_percent,
        recurring_commission_tiers='',
        minimum_topup_kopeks=25000,
        first_topup_bonus_kopeks=0,
        inviter_bonus_kopeks=0,
        max_commission_payments=1,
        max_commission_kopeks=100000,
    )


def _user():
    return SimpleNamespace(
        language='ru',
        referral_reward_preference=None,
        referral_days_subscription_id=None,
    )


@pytest.fixture
def legacy_scheme(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')


@pytest.mark.asyncio
async def test_public_terms_expose_configured_first_payment_percent(legacy_scheme, monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', 0)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT', 100)

    response = await route.get_referral_terms(db=AsyncMock(), user=None)

    assert response.commission_percent == 0
    assert response.first_payment_commission_percent == 100


@pytest.mark.asyncio
async def test_authenticated_terms_keep_commission_fields_independent(legacy_scheme, monkeypatch):
    async def resolve(_db, _user):
        return SimpleNamespace(values=_legacy_values(commission_percent=25, first_payment_commission_percent=50))

    monkeypatch.setattr(route, 'resolve_legacy_referral_settings', resolve)

    response = await route.get_referral_terms(db=AsyncMock(), user=_user())

    assert response.commission_percent == 25
    assert response.first_payment_commission_percent == 50


@pytest.mark.asyncio
async def test_unset_first_payment_percent_uses_effective_commission(legacy_scheme, monkeypatch):
    async def resolve(_db, _user):
        return SimpleNamespace(values=_legacy_values(commission_percent=40, first_payment_commission_percent=None))

    monkeypatch.setattr(route, 'resolve_legacy_referral_settings', resolve)

    response = await route.get_referral_terms(db=AsyncMock(), user=_user())

    assert response.commission_percent == 40
    assert response.first_payment_commission_percent == 40


@pytest.mark.asyncio
async def test_explicit_zero_first_payment_percent_is_preserved(legacy_scheme, monkeypatch):
    async def resolve(_db, _user):
        return SimpleNamespace(values=_legacy_values(commission_percent=40, first_payment_commission_percent=0))

    monkeypatch.setattr(route, 'resolve_legacy_referral_settings', resolve)

    response = await route.get_referral_terms(db=AsyncMock(), user=_user())

    assert response.commission_percent == 40
    assert response.first_payment_commission_percent == 0


@pytest.mark.asyncio
async def test_public_unset_first_payment_percent_uses_global_commission(legacy_scheme, monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', 25)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT', None)

    response = await route.get_referral_terms(db=AsyncMock(), user=None)

    assert response.commission_percent == 25
    assert response.first_payment_commission_percent == 25
