"""[Форк] Тесты фичи «снятие сквадов при истечении подписки».

Проверяем чистую логику (гард free-окна, пред-проверка, восстановление полей, выбор free-сквадов)
и асинхронные handle_expiration (ветки A/B), finalize_expired, restore_squads, идемпотентность —
с замоканными БД и панелью.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import expire_squad_service as svc


def _make_subscription(**kwargs):
    """Лёгкая подписка-заглушка с полями фичи."""
    defaults = dict(
        id=1,
        user_id=42,
        remnawave_id=12345,
        connected_squads=['sq-eu', 'sq-lte'],
        expire_disabled_squads=[],
        expire_free_until=None,
        status='active',
        end_date=datetime(2026, 7, 1, tzinfo=UTC),
        tariff=SimpleNamespace(expire_free_squads=[], expire_free_days=0),
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _fake_db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


# ---------- is_free_window_active ----------


def test_free_window_inactive_when_none():
    assert svc.is_free_window_active(_make_subscription()) is False


def test_free_window_active_when_future():
    now = datetime(2026, 7, 15, tzinfo=UTC)
    sub = _make_subscription(expire_free_until=now + timedelta(days=3))
    assert svc.is_free_window_active(sub, now=now) is True


def test_free_window_inactive_when_past():
    now = datetime(2026, 7, 15, tzinfo=UTC)
    sub = _make_subscription(expire_free_until=now - timedelta(days=1))
    assert svc.is_free_window_active(sub, now=now) is False


def test_free_window_handles_naive_datetime():
    now = datetime(2026, 7, 15, tzinfo=UTC)
    # Наивная дата из pre-TIMESTAMPTZ БД — _aware должен добавить UTC.
    sub = _make_subscription(expire_free_until=datetime(2026, 7, 18))
    assert svc.is_free_window_active(sub, now=now) is True


# ---------- resolve_free_squads / _tariff_free_days ----------


def test_resolve_free_squads_empty_when_days_zero():
    sub = _make_subscription(tariff=SimpleNamespace(expire_free_squads=['sq-free'], expire_free_days=0))
    assert svc.resolve_free_squads(sub) == []


def test_resolve_free_squads_empty_when_no_squads():
    sub = _make_subscription(tariff=SimpleNamespace(expire_free_squads=[], expire_free_days=5))
    assert svc.resolve_free_squads(sub) == []


def test_resolve_free_squads_returns_list_when_both_set():
    sub = _make_subscription(tariff=SimpleNamespace(expire_free_squads=['sq-free'], expire_free_days=5))
    assert svc.resolve_free_squads(sub) == ['sq-free']


def test_resolve_free_squads_no_tariff():
    sub = _make_subscription(tariff=None)
    assert svc.resolve_free_squads(sub) == []


# ---------- should_handle_on_expiry ----------


def test_should_handle_true_with_connected_squads(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    assert svc.should_handle_on_expiry(_make_subscription()) is True


def test_should_handle_true_when_already_disabled(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    sub = _make_subscription(connected_squads=[], expire_disabled_squads=['sq-eu'])
    assert svc.should_handle_on_expiry(sub) is True


def test_should_handle_false_when_feature_off(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: False)
    assert svc.should_handle_on_expiry(_make_subscription()) is False


def test_should_handle_false_when_nothing_connected(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    sub = _make_subscription(connected_squads=[], expire_disabled_squads=[])
    assert svc.should_handle_on_expiry(sub) is False


# ---------- apply_restore_fields ----------


def test_apply_restore_fields_merges_back_without_dups():
    sub = _make_subscription(
        connected_squads=['sq-free'],
        expire_disabled_squads=['sq-eu', 'sq-lte'],
        expire_free_until=datetime(2026, 7, 20, tzinfo=UTC),
    )
    assert svc.apply_restore_fields(sub) is True
    assert sub.connected_squads == ['sq-free', 'sq-eu', 'sq-lte']
    assert sub.expire_disabled_squads == []
    assert sub.expire_free_until is None


def test_apply_restore_fields_noop_when_nothing_disabled():
    sub = _make_subscription()
    assert svc.apply_restore_fields(sub) is False
    assert sub.connected_squads == ['sq-eu', 'sq-lte']


# ---------- handle_expiration (async) ----------


@pytest.mark.anyio
async def test_handle_expiration_branch_a_clears_all(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    monkeypatch.setattr(svc, '_resolve_panel_user_id', lambda sub, user: 12345)
    push = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, '_push_to_panel', push)

    # Тариф без free-сквадов → ветка A.
    sub = _make_subscription()
    handled = await svc.handle_expiration(_fake_db(), SimpleNamespace(remnawave_id=12345), sub)

    assert handled is True
    assert sub.connected_squads == []
    assert sub.expire_disabled_squads == ['sq-eu', 'sq-lte']
    assert sub.expire_free_until is None
    assert sub.status == 'expired'
    push.assert_awaited_once()
    # Пушим пустой список сквадов.
    assert push.await_args.kwargs['active_squads'] == []


@pytest.mark.anyio
async def test_handle_expiration_branch_b_gives_free_squad(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    monkeypatch.setattr(svc, '_resolve_panel_user_id', lambda sub, user: 12345)
    push = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, '_push_to_panel', push)

    now = datetime(2026, 7, 15, tzinfo=UTC)
    sub = _make_subscription(tariff=SimpleNamespace(expire_free_squads=['sq-free'], expire_free_days=7))
    with patch('app.services.expire_squad_service.datetime') as mock_dt:
        mock_dt.now.return_value = now
        handled = await svc.handle_expiration(_fake_db(), SimpleNamespace(remnawave_id=12345), sub)

    assert handled is True
    # Реальные сквады отложены, выданы free-сквады, статус ACTIVE.
    assert sub.connected_squads == ['sq-free']
    assert sub.expire_disabled_squads == ['sq-eu', 'sq-lte']
    assert sub.status == 'active'
    assert sub.expire_free_until == now + timedelta(days=7)
    push.assert_awaited_once()
    assert push.await_args.kwargs['active_squads'] == ['sq-free']


@pytest.mark.anyio
async def test_handle_expiration_disabled_by_flag(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: False)
    sub = _make_subscription()
    handled = await svc.handle_expiration(_fake_db(), SimpleNamespace(), sub)
    assert handled is False
    assert sub.expire_disabled_squads == []


@pytest.mark.anyio
async def test_handle_expiration_nothing_to_do(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    monkeypatch.setattr(svc, '_resolve_panel_user_id', lambda sub, user: 12345)
    # Сквадов нет и free-сквадов нет → нечего делать.
    sub = _make_subscription(connected_squads=[])
    handled = await svc.handle_expiration(_fake_db(), SimpleNamespace(), sub)
    assert handled is False


@pytest.mark.anyio
async def test_handle_expiration_idempotent_repush_branch_a(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    monkeypatch.setattr(svc, '_resolve_panel_user_id', lambda sub, user: 12345)
    push = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, '_push_to_panel', push)

    # Уже обработана веткой A (сквады отложены, free-окна нет) — повторный вызов ре-пушит [].
    sub = _make_subscription(connected_squads=[], expire_disabled_squads=['sq-eu'], expire_free_until=None)
    handled = await svc.handle_expiration(_fake_db(), SimpleNamespace(remnawave_id=12345), sub)
    assert handled is True
    push.assert_awaited_once()
    assert push.await_args.kwargs['active_squads'] == []


@pytest.mark.anyio
async def test_handle_expiration_idempotent_repush_branch_b(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    monkeypatch.setattr(svc, '_resolve_panel_user_id', lambda sub, user: 12345)
    push = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, '_push_to_panel', push)

    now = datetime(2026, 7, 15, tzinfo=UTC)
    # Активное free-окно → повторный вызов ре-пушит free-сквады + панельный expireAt.
    sub = _make_subscription(
        connected_squads=['sq-free'],
        expire_disabled_squads=['sq-eu'],
        expire_free_until=now + timedelta(days=3),
    )
    with patch('app.services.expire_squad_service.datetime') as mock_dt:
        mock_dt.now.return_value = now
        handled = await svc.handle_expiration(_fake_db(), SimpleNamespace(remnawave_id=12345), sub)
    assert handled is True
    push.assert_awaited_once()
    assert push.await_args.kwargs['active_squads'] == ['sq-free']


# ---------- finalize_expired (async) ----------


@pytest.mark.anyio
async def test_finalize_expired_clears_free_squad(monkeypatch):
    push = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, '_push_to_panel', push)
    monkeypatch.setattr(svc, '_resolve_panel_user_id', lambda sub, user: 12345)

    sub = _make_subscription(
        connected_squads=['sq-free'],
        expire_disabled_squads=['sq-eu'],
        expire_free_until=datetime(2026, 7, 10, tzinfo=UTC),
    )
    with patch(
        'app.database.crud.user.get_user_by_id', AsyncMock(return_value=SimpleNamespace(remnawave_id=12345))
    ):
        done = await svc.finalize_expired(_fake_db(), sub)

    assert done is True
    assert sub.connected_squads == []
    assert sub.expire_free_until is None
    assert sub.status == 'expired'
    # expire_disabled_squads сохранены для восстановления.
    assert sub.expire_disabled_squads == ['sq-eu']
    push.assert_awaited_once()
    assert push.await_args.kwargs['active_squads'] == []


@pytest.mark.anyio
async def test_finalize_expired_noop_when_no_free_window():
    sub = _make_subscription(expire_disabled_squads=['sq-eu'], expire_free_until=None)
    done = await svc.finalize_expired(_fake_db(), sub)
    assert done is False


# ---------- restore_squads (async) ----------


@pytest.mark.anyio
async def test_restore_squads_pushes_and_clears(monkeypatch):
    sub = _make_subscription(
        connected_squads=['sq-free'],
        expire_disabled_squads=['sq-eu', 'sq-lte'],
        expire_free_until=datetime(2026, 7, 20, tzinfo=UTC),
    )
    db = _fake_db()

    push = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, '_push_restore_to_panel', push)
    monkeypatch.setattr(svc, '_resolve_panel_user_id', lambda sub, user: 12345)

    with patch(
        'app.database.crud.user.get_user_by_id', AsyncMock(return_value=SimpleNamespace(remnawave_id=12345))
    ):
        restored = await svc.restore_squads(db, sub, reason='test')

    assert restored is True
    assert sub.connected_squads == ['sq-free', 'sq-eu', 'sq-lte']
    assert sub.expire_disabled_squads == []
    assert sub.expire_free_until is None
    push.assert_awaited_once()


@pytest.mark.anyio
async def test_restore_squads_noop_when_nothing_disabled():
    sub = _make_subscription()
    restored = await svc.restore_squads(_fake_db(), sub, reason='test')
    assert restored is False


# ---------- _push_to_panel: статус EXPIRED вычисляемый, его слать нельзя ----------


async def _run_push(status, expire_at):
    """Вызвать _push_to_panel с замоканным api-клиентом, вернуть kwargs update_user."""
    api = MagicMock()
    api.update_user = AsyncMock()
    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=api)
    client_cm.__aexit__ = AsyncMock(return_value=False)
    service = MagicMock()
    service.get_api_client = MagicMock(return_value=client_cm)

    with patch('app.services.subscription_service.SubscriptionService', return_value=service):
        ok = await svc._push_to_panel(
            _make_subscription(),
            12345,
            active_squads=[],
            expire_at=expire_at,
            status=status,
        )
    return ok, api.update_user.await_args.kwargs


@pytest.mark.anyio
async def test_push_to_panel_expired_omits_status_and_past_expire():
    """Ветка A: панель отвергает и status=EXPIRED (вычисляемый), и expireAt в прошлом
    (Validation failed → откат, сквады не снимутся). Поэтому НЕ шлём ни статус, ни
    прошлый expireAt — только пустой список сквадов."""
    from app.database.models import SubscriptionStatus

    past = datetime.now(UTC) - timedelta(days=1)
    ok, kwargs = await _run_push(SubscriptionStatus.EXPIRED, past)
    assert ok is True
    assert kwargs['status'] is None
    assert kwargs['expire_at'] is None
    assert kwargs['active_internal_squads'] == []


@pytest.mark.anyio
async def test_push_to_panel_active_sends_status_and_future_expire():
    """Ветка B: expireAt в будущем — шлём его и статус ACTIVE явно."""
    from app.database.models import SubscriptionStatus
    from app.external.remnawave_api import UserStatus

    future = datetime.now(UTC) + timedelta(days=7)
    ok, kwargs = await _run_push(SubscriptionStatus.ACTIVE, future)
    assert ok is True
    assert kwargs['status'] == UserStatus.ACTIVE
    assert kwargs['expire_at'] == future
