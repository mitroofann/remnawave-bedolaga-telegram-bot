"""[Форк] Тесты интеграции фичи снятия сквадов с SubscriptionStatusMiddleware.

Middleware — самый частый «опережающий» флип ACTIVE→EXPIRED (срабатывает на каждом заходе
юзера после истечения + буфер). Если ветка A применима (фича включена, есть что снимать),
middleware НЕ должен флипать статус напрямую — переход обязан сделать сканер _check_expire_squads
через handle_expiration (иначе сквады останутся висеть на панели). Проверяем этот гард.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import SubscriptionStatus
from app.middlewares.subscription_checker import SubscriptionStatusMiddleware
from app.services import expire_squad_service


def _expired_subscription():
    """ACTIVE-подписка, end_date в прошлом далеко за буфером, с реальными сквадами."""
    return SimpleNamespace(
        id=1,
        user_id=42,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=datetime.now(UTC) - timedelta(hours=1),
        updated_at=None,
        connected_squads=['sq-eu', 'sq-lte'],
        expire_disabled_squads=[],
        expire_free_until=None,
        is_daily_paused=False,
        tariff=None,
    )


def _fake_data(subscription):
    db = MagicMock()
    db.commit = AsyncMock()
    user = SimpleNamespace(id=42, subscriptions=[subscription])
    return {'db': db, 'db_user': user}, db


@pytest.mark.anyio
async def test_middleware_defers_flip_when_branch_a_applies(monkeypatch):
    monkeypatch.setattr(expire_squad_service, 'is_enabled', lambda: True)
    sub = _expired_subscription()
    data, db = _fake_data(sub)
    handler = AsyncMock(return_value='ok')

    result = await SubscriptionStatusMiddleware()(handler, object(), data)

    assert result == 'ok'
    # Фича применима → middleware НЕ флипает, отдаёт переход сканеру.
    assert sub.status == SubscriptionStatus.ACTIVE.value
    db.commit.assert_not_awaited()


@pytest.mark.anyio
async def test_middleware_flips_when_feature_off(monkeypatch):
    monkeypatch.setattr(expire_squad_service, 'is_enabled', lambda: False)
    sub = _expired_subscription()
    data, db = _fake_data(sub)
    handler = AsyncMock(return_value='ok')

    await SubscriptionStatusMiddleware()(handler, object(), data)

    # Фича выключена → штатное поведение сохранено, статус флипается.
    assert sub.status == SubscriptionStatus.EXPIRED.value
    db.commit.assert_awaited_once()
