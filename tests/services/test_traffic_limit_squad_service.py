"""[Форк] Тесты фичи «гашение сквада при исчерпании трафика».

Проверяем чистую логику (какие сквады гасить, какой лимит пушить на панель,
восстановление полей) и асинхронные disable/restore с замоканными БД и панелью.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import traffic_limit_squad_service as svc


_BYTES_PER_GB = 1024 * 1024 * 1024


def _make_subscription(**kwargs):
    """Лёгкая подписка-заглушка с полями фичи."""
    defaults = dict(
        id=1,
        user_id=42,
        remnawave_uuid='sub-uuid',
        connected_squads=['sq-eu', 'sq-lte'],
        traffic_limit_gb=50,
        traffic_limit_disabled_squads=[],
        traffic_limit_panel_bytes=None,
        tariff=SimpleNamespace(limit_disabled_squads=['sq-lte']),
        end_date=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------- squads_to_disable ----------


def test_squads_to_disable_intersects_connected_and_tariff():
    sub = _make_subscription()
    assert svc.squads_to_disable(sub) == ['sq-lte']


def test_squads_to_disable_empty_when_tariff_not_configured():
    sub = _make_subscription(tariff=SimpleNamespace(limit_disabled_squads=[]))
    assert svc.squads_to_disable(sub) == []


def test_squads_to_disable_empty_when_squad_not_connected():
    # Лимит-сквад настроен в тарифе, но у юзера сейчас не подключён.
    sub = _make_subscription(connected_squads=['sq-eu'])
    assert svc.squads_to_disable(sub) == []


def test_squads_to_disable_no_tariff():
    sub = _make_subscription(tariff=None)
    assert svc.squads_to_disable(sub) == []


# ---------- panel_traffic_limit_bytes (гард) ----------


def test_panel_limit_uses_tariff_when_no_disabled_squads():
    sub = _make_subscription()
    assert svc.panel_traffic_limit_bytes(sub) == 50 * _BYTES_PER_GB


def test_panel_limit_uses_raised_value_while_disabled():
    sub = _make_subscription(
        traffic_limit_disabled_squads=['sq-lte'],
        traffic_limit_panel_bytes=12345,
    )
    # Пока сквад погашен — пушим сохранённый поднятый лимит, а не тарифный.
    assert svc.panel_traffic_limit_bytes(sub) == 12345


def test_panel_limit_unlimited_tariff():
    sub = _make_subscription(traffic_limit_gb=0)
    assert svc.panel_traffic_limit_bytes(sub) == 0


# ---------- apply_restore_fields ----------


def test_apply_restore_fields_merges_back_without_dups():
    sub = _make_subscription(
        connected_squads=['sq-eu'],
        traffic_limit_disabled_squads=['sq-lte'],
        traffic_limit_panel_bytes=999,
    )
    assert svc.apply_restore_fields(sub) is True
    assert sub.connected_squads == ['sq-eu', 'sq-lte']
    assert sub.traffic_limit_disabled_squads == []
    assert sub.traffic_limit_panel_bytes is None


def test_apply_restore_fields_noop_when_nothing_disabled():
    sub = _make_subscription()
    assert svc.apply_restore_fields(sub) is False
    assert sub.connected_squads == ['sq-eu', 'sq-lte']


# ---------- disable_squads_on_limit (async) ----------


def _fake_db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.mark.anyio
async def test_disable_squads_moves_squad_and_raises_limit(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    sub = _make_subscription()
    user = SimpleNamespace(remnawave_uuid='sub-uuid')
    db = _fake_db()

    push = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, '_push_to_panel', push)
    monkeypatch.setattr(svc, '_resolve_panel_uuid', lambda sub, user: 'sub-uuid')

    # used = 50 ГБ → панельный лимит = used + 10КБ.
    used_bytes = 50 * _BYTES_PER_GB
    handled = await svc.disable_squads_on_limit(db, user, sub, used_bytes=used_bytes)

    assert handled is True
    # Лимит-сквад снят из connected, перенесён в disabled.
    assert sub.connected_squads == ['sq-eu']
    assert sub.traffic_limit_disabled_squads == ['sq-lte']
    # Панельный лимит = used + буфер, тарифный traffic_limit_gb НЕ тронут.
    assert sub.traffic_limit_panel_bytes == used_bytes + 10 * 1024
    assert sub.traffic_limit_gb == 50
    push.assert_awaited_once()


@pytest.mark.anyio
async def test_disable_squads_disabled_by_flag(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: False)
    sub = _make_subscription()
    handled = await svc.disable_squads_on_limit(_fake_db(), SimpleNamespace(), sub)
    assert handled is False
    assert sub.traffic_limit_disabled_squads == []


@pytest.mark.anyio
async def test_disable_squads_nothing_to_disable(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    sub = _make_subscription(tariff=SimpleNamespace(limit_disabled_squads=[]))
    handled = await svc.disable_squads_on_limit(_fake_db(), SimpleNamespace(), sub)
    assert handled is False


@pytest.mark.anyio
async def test_disable_squads_idempotent_when_already_disabled(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    push = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, '_push_to_panel', push)
    monkeypatch.setattr(svc, '_resolve_panel_uuid', lambda sub, user: 'sub-uuid')
    sub = _make_subscription(
        connected_squads=['sq-eu'],
        traffic_limit_disabled_squads=['sq-lte'],
        traffic_limit_panel_bytes=123,
    )
    # Повторный вебхук: сквады уже погашены — возвращаем True (не переводить в LIMITED),
    # но панель повторно не трогаем.
    handled = await svc.disable_squads_on_limit(_fake_db(), SimpleNamespace(), sub)
    assert handled is True
    push.assert_not_awaited()


@pytest.mark.anyio
async def test_restore_squads_pushes_and_clears(monkeypatch):
    sub = _make_subscription(
        connected_squads=['sq-eu'],
        traffic_limit_disabled_squads=['sq-lte'],
        traffic_limit_panel_bytes=999,
    )
    db = _fake_db()

    fake_service = MagicMock()
    fake_service.update_remnawave_user = AsyncMock(return_value=object())
    fake_service.enable_remnawave_user = AsyncMock(return_value=True)

    monkeypatch.setattr(svc, '_resolve_panel_uuid', lambda sub, user: 'sub-uuid')

    with patch('app.services.subscription_service.SubscriptionService', return_value=fake_service), patch(
        'app.database.crud.user.get_user_by_id',
        AsyncMock(return_value=SimpleNamespace(remnawave_uuid='sub-uuid')),
    ):
        restored = await svc.restore_squads(db, sub, reason='test')

    assert restored is True
    assert sub.connected_squads == ['sq-eu', 'sq-lte']
    assert sub.traffic_limit_disabled_squads == []
    assert sub.traffic_limit_panel_bytes is None
    fake_service.update_remnawave_user.assert_awaited_once()


@pytest.mark.anyio
async def test_restore_squads_noop_when_nothing_disabled():
    sub = _make_subscription()
    restored = await svc.restore_squads(_fake_db(), sub, reason='test')
    assert restored is False
