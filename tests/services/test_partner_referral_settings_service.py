from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import partner_referral_settings_service as service


@pytest.fixture
def global_settings(monkeypatch):
    values = {
        'REFERRAL_MINIMUM_TOPUP_KOPEKS': 1000,
        'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS': 200,
        'REFERRAL_INVITER_BONUS_KOPEKS': 300,
        'REFERRAL_COMMISSION_PERCENT': 15,
        'REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT': 100,
        'REFERRAL_RECURRING_COMMISSION_TIERS': '0:50,10:25',
        'REFERRAL_MAX_COMMISSION_PAYMENTS': 2,
        'REFERRAL_MAX_COMMISSION_KOPEKS': 5000,
    }
    for name, value in values.items():
        monkeypatch.setattr(service.settings, name, value)
    return values


def _user(*, is_partner: bool, commission: int | None = None):
    return SimpleNamespace(id=7, is_partner=is_partner, referral_commission_percent=commission)


@pytest.mark.asyncio
async def test_ordinary_referrer_ignores_legacy_profile_percent(global_settings):
    db = SimpleNamespace(execute=AsyncMock())

    resolved = await service.resolve_legacy_referral_settings(db, _user(is_partner=False, commission=99))

    assert resolved.values.commission_percent == global_settings['REFERRAL_COMMISSION_PERCENT']
    assert resolved.sources['commission_percent'] == 'global'
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_approved_partner_applies_nullable_overrides(global_settings):
    row = SimpleNamespace(
        minimum_topup_kopeks=0,
        first_topup_bonus_kopeks=None,
        inviter_bonus_kopeks=900,
        commission_percent=40,
        first_payment_commission_percent=None,
        recurring_commission_tiers='',
        max_commission_payments=0,
        max_commission_kopeks=None,
    )
    result = SimpleNamespace(scalar_one_or_none=lambda: row)
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    resolved = await service.resolve_legacy_referral_settings(db, _user(is_partner=True, commission=30))

    assert resolved.values.minimum_topup_kopeks == 0
    assert resolved.values.first_topup_bonus_kopeks == global_settings['REFERRAL_FIRST_TOPUP_BONUS_KOPEKS']
    assert resolved.values.inviter_bonus_kopeks == 900
    assert resolved.values.commission_percent == 40
    assert resolved.values.recurring_commission_tiers == ''
    assert resolved.values.max_commission_payments == 0
    assert resolved.sources['minimum_topup_kopeks'] == 'partner'
    assert resolved.sources['first_topup_bonus_kopeks'] == 'global'


@pytest.mark.asyncio
async def test_revoked_partner_row_is_inactive(global_settings):
    row = SimpleNamespace(commission_percent=99)
    result = SimpleNamespace(scalar_one_or_none=lambda: row)
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    resolved = await service.resolve_legacy_referral_settings(db, _user(is_partner=False, commission=99))

    assert resolved.values.commission_percent == global_settings['REFERRAL_COMMISSION_PERCENT']
    assert resolved.sources['commission_percent'] == 'global'
    db.execute.assert_not_awaited()
