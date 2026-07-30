"""Тесты изолированной фичи «триал через лендинг-воронку».

Проверяют гейты доступности (env kill-switch, флаг лендинга, тип аккаунта,
«один триал на контакт») и резолвер параметров триала. Бизнес-логику создания
юзера/подписки не дублируем — она уже покрыта существующими тестами
guest_purchase / trial; здесь пиним именно новую обвязку.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import landing_trial_service as lts
from app.services.guest_purchase_service import GuestPurchaseError


def _landing(trial_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(slug='promo', trial_enabled=trial_enabled)


# ---------- гейт: глобальный env kill-switch + флаг лендинга ----------


def test_globally_enabled_reads_settings():
    with patch.object(lts.settings, 'LANDING_TRIAL_ENABLED', True):
        assert lts.is_landing_trial_globally_enabled() is True
    with patch.object(lts.settings, 'LANDING_TRIAL_ENABLED', False):
        assert lts.is_landing_trial_globally_enabled() is False


def test_assert_enabled_raises_when_env_off():
    with patch.object(lts.settings, 'LANDING_TRIAL_ENABLED', False):
        with pytest.raises(GuestPurchaseError) as exc:
            lts._assert_landing_trial_enabled(_landing(trial_enabled=True))
    assert exc.value.status_code == 403


def test_assert_enabled_raises_when_landing_flag_off():
    with patch.object(lts.settings, 'LANDING_TRIAL_ENABLED', True):
        with pytest.raises(GuestPurchaseError) as exc:
            lts._assert_landing_trial_enabled(_landing(trial_enabled=False))
    assert exc.value.status_code == 403


def test_assert_enabled_passes_when_both_on():
    with patch.object(lts.settings, 'LANDING_TRIAL_ENABLED', True):
        lts._assert_landing_trial_enabled(_landing(trial_enabled=True))  # no raise


# ---------- гейт: тип аккаунта (TRIAL_DISABLED_FOR) ----------


def test_assert_contact_allowed_blocks_by_type():
    with patch.object(type(lts.settings), 'is_trial_disabled_for_user', lambda self, t: True):
        with pytest.raises(GuestPurchaseError) as exc:
            lts._assert_contact_allowed('email')
    assert exc.value.status_code == 400


def test_assert_contact_allowed_passes():
    with patch.object(type(lts.settings), 'is_trial_disabled_for_user', lambda self, t: False):
        lts._assert_contact_allowed('telegram')  # no raise


# ---------- гейт: «один триал на контакт» ----------


@pytest.mark.asyncio
async def test_trial_not_used_passes_for_unknown_contact():
    db = AsyncMock()
    with patch.object(lts, '_find_existing_user_by_contact', AsyncMock(return_value=None)):
        await lts._assert_trial_not_used(db, 'email', 'new@example.com')  # no raise


@pytest.mark.asyncio
async def test_trial_not_used_raises_409_when_already_used():
    db = AsyncMock()
    db.refresh = AsyncMock()
    existing = MagicMock()
    existing.is_trial_already_used.return_value = True
    with patch.object(lts, '_find_existing_user_by_contact', AsyncMock(return_value=existing)):
        with pytest.raises(GuestPurchaseError) as exc:
            await lts._assert_trial_not_used(db, 'email', 'used@example.com')
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_trial_not_used_passes_when_existing_user_never_took_trial():
    db = AsyncMock()
    db.refresh = AsyncMock()
    existing = MagicMock()
    existing.is_trial_already_used.return_value = False
    with patch.object(lts, '_find_existing_user_by_contact', AsyncMock(return_value=existing)):
        await lts._assert_trial_not_used(db, 'email', 'fresh@example.com')  # no raise


# ---------- публичный конфиг лендинга ----------


@pytest.mark.asyncio
async def test_trial_info_none_when_globally_disabled():
    db = AsyncMock()
    with patch.object(lts.settings, 'LANDING_TRIAL_ENABLED', False):
        info = await lts.get_landing_trial_info(db, _landing(trial_enabled=True))
    assert info is None


@pytest.mark.asyncio
async def test_trial_info_none_when_landing_flag_off():
    db = AsyncMock()
    with patch.object(lts.settings, 'LANDING_TRIAL_ENABLED', True):
        info = await lts.get_landing_trial_info(db, _landing(trial_enabled=False))
    assert info is None


@pytest.mark.asyncio
async def test_trial_info_returns_params_when_enabled():
    db = AsyncMock()
    fake_params = lts.TrialParams(
        duration_days=3,
        traffic_limit_gb=10,
        device_limit=2,
        requires_payment=True,
        price_kopeks=1000,
        tariff_id=5,
        squads=['sq'],
    )
    with (
        patch.object(lts.settings, 'LANDING_TRIAL_ENABLED', True),
        patch.object(lts, 'resolve_landing_trial_params', AsyncMock(return_value=fake_params)),
    ):
        info = await lts.get_landing_trial_info(db, _landing(trial_enabled=True))
    assert info == {
        'enabled': True,
        'duration_days': 3,
        'traffic_limit_gb': 10,
        'device_limit': 2,
        'requires_payment': True,
        'price_kopeks': 1000,
        'price_rubles': 10.0,
    }


# ---------- платный триал: гейт «фича бесплатна» ----------


@pytest.mark.asyncio
async def test_start_paid_trial_rejects_when_free():
    db = AsyncMock()
    free_params = lts.TrialParams(
        duration_days=3,
        traffic_limit_gb=10,
        device_limit=2,
        requires_payment=False,
        price_kopeks=0,
        tariff_id=5,
        squads=[],
    )
    with (
        patch.object(lts.settings, 'LANDING_TRIAL_ENABLED', True),
        patch.object(type(lts.settings), 'is_trial_disabled_for_user', lambda self, t: False),
        patch.object(lts, '_assert_trial_not_used', AsyncMock(return_value=None)),
        patch.object(lts, 'resolve_landing_trial_params', AsyncMock(return_value=free_params)),
    ):
        with pytest.raises(GuestPurchaseError) as exc:
            await lts.start_paid_trial(
                db,
                _landing(True),
                contact_type='email',
                contact_value='a@b.com',
                payment_method='yookassa',
                return_url='https://x/{token}',
            )
    assert exc.value.status_code == 400
