"""[Форк] Тесты фичи «гашение сквада при исчерпании трафика».

Проверяем чистую логику (какие сквады гасить, какой лимит пушить на панель,
восстановление полей) и асинхронные disable/restore с замоканными БД и панелью.
"""

from datetime import UTC, datetime
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
        remnawave_id=12345,
        connected_squads=['sq-eu', 'sq-lte'],
        traffic_limit_gb=50,
        traffic_limit_disabled_squads=[],
        traffic_limit_panel_bytes=None,
        tariff=SimpleNamespace(limit_disabled_squads=['sq-lte']),
        end_date=None,
        start_date=None,
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


def test_panel_limit_is_unlimited_while_disabled():
    # Пока сквад погашен — на панели БЕЗЛИМИТ (0), иначе любой лимит пробивается на
    # оставшихся серверах и панель зацикливает LIMITED. traffic_limit_panel_bytes при
    # этом не влияет на результат.
    sub = _make_subscription(
        traffic_limit_disabled_squads=['sq-lte'],
        traffic_limit_panel_bytes=12345,
    )
    assert svc.panel_traffic_limit_bytes(sub) == 0


def test_panel_limit_unlimited_tariff():
    sub = _make_subscription(traffic_limit_gb=0)
    assert svc.panel_traffic_limit_bytes(sub) == 0


# ---------- days_until_traffic_reset ----------


def _sub_with_mode(mode, start=None):
    return _make_subscription(
        tariff=SimpleNamespace(limit_disabled_squads=['sq-lte'], traffic_reset_mode=mode),
        start_date=start,
    )


def test_reset_days_day_mode():
    # DAY: сброс каждую полночь → сегодня остаётся 1 день до следующего сброса.
    now = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    assert svc.days_until_traffic_reset(_sub_with_mode('DAY'), now=now) == 1


def test_reset_days_month_mode_midmonth():
    # MONTH: 1-е число следующего месяца. 15 июля → до 1 августа 17 дней.
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    assert svc.days_until_traffic_reset(_sub_with_mode('MONTH'), now=now) == 17


def test_reset_days_month_mode_december_rollover():
    # MONTH через границу года: 20 дек → до 1 января 12 дней.
    now = datetime(2026, 12, 20, 0, 0, tzinfo=UTC)
    assert svc.days_until_traffic_reset(_sub_with_mode('MONTH'), now=now) == 12


def test_reset_days_week_mode():
    # WEEK: следующий понедельник. 2026-07-15 — среда (weekday=2) → до пн 20-го 5 дней.
    now = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
    assert svc.days_until_traffic_reset(_sub_with_mode('WEEK'), now=now) == 5


def test_reset_days_week_mode_on_monday():
    # Понедельник → следующий сброс через неделю (не сегодня).
    now = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)  # понедельник
    assert svc.days_until_traffic_reset(_sub_with_mode('WEEK'), now=now) == 7


def test_reset_days_month_rolling():
    # MONTH_ROLLING: 30-дневные циклы от start_date. Прошло 10 дней → осталось 20.
    start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
    assert svc.days_until_traffic_reset(_sub_with_mode('MONTH_ROLLING', start=start), now=now) == 20


def test_reset_days_no_reset_returns_none():
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    assert svc.days_until_traffic_reset(_sub_with_mode('NO_RESET'), now=now) is None


def test_reset_days_defaults_to_month_when_mode_absent():
    # Тариф без traffic_reset_mode → глобальный дефолт (MONTH).
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    sub = _make_subscription(tariff=SimpleNamespace(limit_disabled_squads=['sq-lte'], traffic_reset_mode=None))
    assert svc.days_until_traffic_reset(sub, now=now) == 17


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


# ---------- should_disable_on_panel_limit (sync-развилка) ----------


def test_should_disable_true_when_squads_to_disable(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    assert svc.should_disable_on_panel_limit(_make_subscription()) is True


def test_should_disable_true_when_already_disabled(monkeypatch):
    # Панель ещё догоняет и шлёт LIMITED, но сквады уже сняты — статус не трогаем.
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    sub = _make_subscription(connected_squads=['sq-eu'], traffic_limit_disabled_squads=['sq-lte'])
    assert svc.should_disable_on_panel_limit(sub) is True


def test_should_disable_false_when_feature_off(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: False)
    assert svc.should_disable_on_panel_limit(_make_subscription()) is False


def test_should_disable_false_when_tariff_not_configured(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    sub = _make_subscription(tariff=SimpleNamespace(limit_disabled_squads=[]))
    assert svc.should_disable_on_panel_limit(sub) is False


@pytest.mark.anyio
async def test_disable_squads_moves_squad_and_sets_unlimited(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    sub = _make_subscription()
    user = SimpleNamespace(remnawave_id=12345)
    db = _fake_db()

    push = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, '_push_to_panel', push)
    monkeypatch.setattr(svc, '_resolve_panel_user_id', lambda sub, user: 12345)

    handled = await svc.disable_squads_on_limit(db, user, sub)

    assert handled is True
    # Лимит-сквад снят из connected, перенесён в disabled.
    assert sub.connected_squads == ['sq-eu']
    assert sub.traffic_limit_disabled_squads == ['sq-lte']
    # На панели БЕЗЛИМИТ (маркер 0), тарифный traffic_limit_gb НЕ тронут.
    assert sub.traffic_limit_panel_bytes == 0
    assert svc.panel_traffic_limit_bytes(sub) == 0
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
async def test_disable_squads_repushes_unlimited_when_already_disabled(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    push = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, '_push_to_panel', push)
    monkeypatch.setattr(svc, '_resolve_panel_user_id', lambda sub, user: 12345)
    # Застрявшая подписка: сквады погашены, но на панели ещё старый ненулевой лимит,
    # панель шлёт LIMITED повторно.
    sub = _make_subscription(
        connected_squads=['sq-eu'],
        traffic_limit_disabled_squads=['sq-lte'],
        traffic_limit_panel_bytes=123,
    )
    handled = await svc.disable_squads_on_limit(_fake_db(), SimpleNamespace(), sub)
    assert handled is True
    # Ненулевой лимит обнулён (→ panel_traffic_limit_bytes вернёт безлимит) и перепушен + enable.
    assert sub.traffic_limit_panel_bytes == 0
    push.assert_awaited_once()


@pytest.mark.anyio
async def test_disable_squads_repush_when_already_unlimited(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    push = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, '_push_to_panel', push)
    monkeypatch.setattr(svc, '_resolve_panel_user_id', lambda sub, user: 12345)
    # Повторный вебхук на уже корректно погашенной подписке (лимит уже 0).
    sub = _make_subscription(
        connected_squads=['sq-eu'],
        traffic_limit_disabled_squads=['sq-lte'],
        traffic_limit_panel_bytes=0,
    )
    handled = await svc.disable_squads_on_limit(_fake_db(), SimpleNamespace(), sub)
    assert handled is True
    # Всё равно перепушиваем безлимит + enable, чтобы снять LIMITED на панели.
    push.assert_awaited_once()


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

    monkeypatch.setattr(svc, '_resolve_panel_user_id', lambda sub, user: 12345)

    with (
        patch('app.services.subscription_service.SubscriptionService', return_value=fake_service),
        patch(
            'app.database.crud.user.get_user_by_id',
            AsyncMock(return_value=SimpleNamespace(remnawave_id=12345)),
        ),
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
