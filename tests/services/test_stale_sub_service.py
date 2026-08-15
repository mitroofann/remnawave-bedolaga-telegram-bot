"""[Форк] Тесты фичи «уведомления о несвежей подписке» (stale-sub notifications).

Проверяем чистую логику (classify_device, filter_candidates, рекомендация Happ/INCY,
кулдаун, сборка текста, время суточной проверки) и асинхронный сбор кандидатов
(collect_candidates) с замоканными БД и панелью.
"""

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import stale_sub_service as svc


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _make_device(**kwargs):
    """Устройство RemnaWave (get_user_devices_all)."""
    defaults = dict(
        hwid='hwid-1',
        deviceModel='Android/13',
        userAgent='Happ/4.1.0/Android/17860740401641591541',
        lastSeen='2026-08-10T10:00:00Z',
        updatedAt='2026-08-10T10:00:00Z',
        createdAt='2026-01-01T00:00:00Z',
    )
    defaults.update(kwargs)
    return defaults


def _history_record(**kwargs):
    """Запись истории запросов подписки (get_subscription_request_history)."""
    defaults = dict(id=1, userId=12345, requestAt='2026-08-01T00:00:00Z', requestIp='1.2.3.4', userAgent='Happ/4.1.0')
    defaults.update(kwargs)
    return defaults


def _fake_db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


# ---------- _parse_datetime_or_none ----------


def test_parse_datetime_handles_z_suffix():
    parsed = svc._parse_datetime_or_none('2026-08-10T10:00:00Z')
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.hour == 10


def test_parse_datetime_none_for_garbage():
    assert svc._parse_datetime_or_none(None) is None
    assert svc._parse_datetime_or_none('not-a-date') is None


# ---------- _check_time_due ----------


def test_check_time_due_matches():
    assert svc._check_time_due(NOW, time(12, 0)) is True


def test_check_time_due_not_matched():
    assert svc._check_time_due(NOW, time(11, 0)) is False
    assert svc._check_time_due(NOW, time(12, 1)) is False


def test_check_time_due_none():
    assert svc._check_time_due(NOW, None) is False


# ---------- _matches_app_recommendation ----------


def test_app_recommendation_true_when_no_happ_incy():
    assert svc._matches_app_recommendation(['Android/13']) is True


def test_app_recommendation_false_when_happ():
    assert svc._matches_app_recommendation(['Happ/4.1.0/Android']) is False


def test_app_recommendation_false_when_incy_case_insensitive():
    assert svc._matches_app_recommendation(['incy/3.5.3/android']) is False


def test_app_recommendation_false_when_any_device_is_happ():
    assert svc._matches_app_recommendation(['Android/13', 'Happ/4.1.0']) is False


# ---------- _join_device_names ----------


def test_join_device_names():
    assert svc._join_device_names(['A', 'B', 'C']) == 'A, B, C'


def test_join_device_names_skips_empty():
    assert svc._join_device_names(['A', '', 'C']) == 'A, C'


# ---------- classify_device ----------


def test_classify_device_candidate():
    device = _make_device(
        lastSeen='2026-08-10T10:00:00Z',  # 2ч назад — недавно
        userAgent='Happ/4.1.0',
    )
    history = {'Happ/4.1.0': datetime(2026, 8, 1, tzinfo=UTC)}  # 9 дней назад
    stale = svc.classify_device(device, history, now=NOW, last_seen_hours=24, last_request_hours=72)
    assert stale is not None
    assert stale.name == 'Android/13'
    assert stale.last_seen.hour == 10
    assert stale.last_request == datetime(2026, 8, 1, tzinfo=UTC)


def test_classify_device_skips_when_not_recently_seen():
    device = _make_device(
        lastSeen='2026-08-01T10:00:00Z',  # 9 дней назад — давно не был онлайн
        userAgent='Happ/4.1.0',
    )
    history = {'Happ/4.1.0': datetime(2026, 8, 1, tzinfo=UTC)}
    assert svc.classify_device(device, history, now=NOW, last_seen_hours=24, last_request_hours=72) is None


def test_classify_device_skips_when_recently_requested():
    device = _make_device(
        lastSeen='2026-08-10T10:00:00Z',
        userAgent='Happ/4.1.0',
    )
    history = {'Happ/4.1.0': datetime(2026, 8, 10, 9, tzinfo=UTC)}  # час назад — свежая
    assert svc.classify_device(device, history, now=NOW, last_seen_hours=24, last_request_hours=72) is None


def test_classify_device_skips_when_no_last_seen():
    device = _make_device(lastSeen=None, updatedAt=None)
    assert svc.classify_device(device, {}, now=NOW, last_seen_hours=24, last_request_hours=72) is None


def test_classify_device_skips_when_no_history_match():
    device = _make_device(lastSeen='2026-08-10T10:00:00Z', userAgent='OtherApp/1.0')
    history = {'Happ/4.1.0': datetime(2026, 8, 1, tzinfo=UTC)}
    assert svc.classify_device(device, history, now=NOW, last_seen_hours=24, last_request_hours=72) is None


def test_classify_device_threshold_zero_disabled():
    # last_seen_hours=0 / last_request_hours=0 → пороги выключены: кандидат по любой дате.
    device = _make_device(
        lastSeen='2026-08-01T10:00:00Z',
        userAgent='Happ/4.1.0',
    )
    history = {'Happ/4.1.0': datetime(2026, 8, 1, tzinfo=UTC)}
    stale = svc.classify_device(device, history, now=NOW, last_seen_hours=0, last_request_hours=0)
    assert stale is not None


# ---------- filter_candidates ----------


def test_filter_candidates_sorts_by_last_seen_desc():
    devices = [
        # Оба устройства свежие (в пределах 24ч), подписка у обоих давно не запрашивалась.
        _make_device(hwid='old', deviceModel='Old/1', lastSeen='2026-08-10T08:00:00Z', userAgent='A'),
        _make_device(hwid='new', deviceModel='New/1', lastSeen='2026-08-10T11:00:00Z', userAgent='A'),
    ]
    history = {'A': datetime(2026, 8, 1, tzinfo=UTC)}
    result = svc.filter_candidates(devices, history, now=NOW, last_seen_hours=24, last_request_hours=72, recommend=True)
    assert [d.hwid for d in result] == ['new', 'old']


def test_filter_candidates_drops_happ_when_recommend_false():
    devices = [
        _make_device(hwid='h1', deviceModel='Happ/4.1.0', lastSeen='2026-08-10T10:00:00Z', userAgent='Happ'),
        _make_device(hwid='h2', deviceModel='Android/13', lastSeen='2026-08-10T10:00:00Z', userAgent='A'),
    ]
    history = {'Happ': datetime(2026, 8, 1, tzinfo=UTC), 'A': datetime(2026, 8, 1, tzinfo=UTC)}
    result = svc.filter_candidates(
        devices, history, now=NOW, last_seen_hours=24, last_request_hours=72, recommend=False
    )
    assert [d.hwid for d in result] == ['h2']


# ---------- should_skip_repeat ----------


def test_should_skip_repeat_fresh_row():
    row = SimpleNamespace(created_at=NOW - timedelta(days=2))
    assert svc.should_skip_repeat(row, now=NOW, cooldown_days=5) is True


def test_should_skip_repeat_old_row():
    row = SimpleNamespace(created_at=NOW - timedelta(days=10))
    assert svc.should_skip_repeat(row, now=NOW, cooldown_days=5) is False


def test_should_skip_repeat_no_row():
    assert svc.should_skip_repeat(None, now=NOW, cooldown_days=5) is False


def test_should_skip_repeat_zero_cooldown():
    # cooldown=0: окно «не слать повторно» схлопывается в ноль — запись устаревает сразу.
    row = SimpleNamespace(created_at=NOW - timedelta(days=365))
    assert svc.should_skip_repeat(row, now=NOW, cooldown_days=0) is False


# ---------- build_message ----------


def _fake_texts():
    return SimpleNamespace(
        t=lambda key, default: {
            'STALE_SUB_SINGLE': 'Устройство {devices} не обновлялось.',
            'STALE_SUB_MULTI': 'Устройства {devices} не обновлялись.',
            'STALE_SUB_RECOMMEND': '\n\nРекомендуем Happ или INCY.',
        }.get(key, default)
    )


def test_build_message_single():
    message = svc.build_message(['Android/13'], False, texts=_fake_texts())
    assert message == 'Устройство Android/13 не обновлялось.'


def test_build_message_multi():
    message = svc.build_message(['A', 'B'], False, texts=_fake_texts())
    assert message == 'Устройства A, B не обновлялись.'


def test_build_message_with_recommendation():
    message = svc.build_message(['Android/13'], True, texts=_fake_texts())
    assert message.endswith('Рекомендуем Happ или INCY.')


# ---------- collect_candidates (async) ----------


def _fake_api():
    api = MagicMock()
    api.get_user_devices_all = AsyncMock(return_value={'devices': [], 'total': 0})
    api.get_subscription_request_history = AsyncMock(return_value={'total': 0, 'records': []})
    return api


def _make_subscription(**kwargs):
    defaults = dict(
        id=1,
        user_id=42,
        remnawave_id=12345,
        status='active',
        tariff=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_user(**kwargs):
    defaults = dict(id=42, remnawave_id=12345, status='active', telegram_id=None, language='ru')
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.mark.anyio
async def test_collect_candidates_returns_matching_subscription(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    monkeypatch.setattr(svc, '_resolve_panel_user_id', lambda sub, user: 12345)

    api = _fake_api()
    api.get_user_devices_all = AsyncMock(
        return_value={
            'devices': [_make_device(lastSeen='2026-08-10T10:00:00Z', userAgent='Happ/4.1.0')],
            'total': 1,
        }
    )
    api.get_subscription_request_history = AsyncMock(
        return_value={'total': 1, 'records': [_history_record(userAgent='Happ/4.1.0')]}
    )
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=api)
    cm.__aexit__ = AsyncMock(return_value=False)
    service = MagicMock()
    service.get_api_client = MagicMock(return_value=cm)
    monkeypatch.setattr(svc, 'SubscriptionService', lambda: service)

    db = _fake_db()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [_make_subscription(user=_make_user())]
    db.execute = AsyncMock(return_value=result)

    monkeypatch.setattr(svc.settings, 'NOTIFY_STALE_SUB_LAST_SEEN_HOURS', 24)
    monkeypatch.setattr(svc.settings, 'NOTIFY_STALE_SUB_LAST_REQUEST_HOURS', 72)

    candidates = await svc.collect_candidates(db, now=NOW)
    assert len(candidates) == 1
    user, sub, stale_devices = candidates[0]
    assert user.id == 42
    assert stale_devices[0].name == 'Android/13'


@pytest.mark.anyio
async def test_collect_candidates_skips_when_no_matching_devices(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    monkeypatch.setattr(svc, '_resolve_panel_user_id', lambda sub, user: 12345)

    api = _fake_api()
    api.get_user_devices_all = AsyncMock(
        return_value={'devices': [_make_device(lastSeen='2026-08-10T10:00:00Z', userAgent='Happ/4.1.0')], 'total': 1}
    )
    # История свежая (час назад) → не кандидат.
    api.get_subscription_request_history = AsyncMock(
        return_value={
            'total': 1,
            'records': [_history_record(requestAt='2026-08-10T11:00:00Z', userAgent='Happ/4.1.0')],
        }
    )
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=api)
    cm.__aexit__ = AsyncMock(return_value=False)
    service = MagicMock()
    service.get_api_client = MagicMock(return_value=cm)
    monkeypatch.setattr(svc, 'SubscriptionService', lambda: service)

    db = _fake_db()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [_make_subscription(user=_make_user())]
    db.execute = AsyncMock(return_value=result)

    monkeypatch.setattr(svc.settings, 'NOTIFY_STALE_SUB_LAST_SEEN_HOURS', 24)
    monkeypatch.setattr(svc.settings, 'NOTIFY_STALE_SUB_LAST_REQUEST_HOURS', 72)

    candidates = await svc.collect_candidates(db, now=NOW)
    assert candidates == []


@pytest.mark.anyio
async def test_collect_candidates_handles_api_error(monkeypatch):
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    monkeypatch.setattr(svc, '_resolve_panel_user_id', lambda sub, user: 12345)

    api = _fake_api()
    api.get_user_devices_all = AsyncMock(side_effect=Exception('panel down'))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=api)
    cm.__aexit__ = AsyncMock(return_value=False)
    service = MagicMock()
    service.get_api_client = MagicMock(return_value=cm)
    monkeypatch.setattr(svc, 'SubscriptionService', lambda: service)

    db = _fake_db()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [_make_subscription(user=_make_user())]
    db.execute = AsyncMock(return_value=result)

    monkeypatch.setattr(svc.settings, 'NOTIFY_STALE_SUB_LAST_SEEN_HOURS', 24)
    monkeypatch.setattr(svc.settings, 'NOTIFY_STALE_SUB_LAST_REQUEST_HOURS', 72)

    # Ошибка панели не должна ронять всю проверку.
    candidates = await svc.collect_candidates(db, now=NOW)
    assert candidates == []
