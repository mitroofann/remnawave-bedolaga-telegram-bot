"""[Форк] Regression: сквады, восстановленные продлением, не должны снова сниматься
запоздалым/повторным вебхуком истечения (user.expired / user.modified EXPIRED).

Сценарий юзера: фича бесплатных серверов выключена (expire_free_days=0) → при истечении
срабатывает ветка A (снять ВСЕ сквады, отложить в expire_disabled_squads). После продления
сквады восстановлены (apply_restore_fields), expire_disabled_squads очищен, статус ACTIVE,
end_date в будущем. Панель продолжает слать ретраи истечения (user.expired / user.modified
с УСТАРЕВШИМ снимком: status=EXPIRED). Без гарда _handle_expire_squads решает, что это новое
истечение (should_handle_on_expiry → True по непустым connected_squads), и снова снимает
сквады — юзер видит «при продлении сквады не возвращаются».

Гард №1 (чистая логика): should_handle_on_expiry не должен быть True для живой подписки.
Гард №2 (вебхук): _handle_user_expired игнорирует устаревший ретрай, как _handle_user_modified.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import expire_squad_service as svc
from app.services.remnawave_webhook_service import RemnaWaveWebhookService
from tests.services.test_expire_squad_service import _make_subscription


# ---------- Гард №1: should_handle_on_expiry ----------


def test_should_handle_false_when_end_date_in_future(monkeypatch):
    """Живая подписка (end_date в будущем, сквады подключены) НЕ кандидат на снятие сквадов."""
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    sub = _make_subscription(end_date=datetime.now(UTC) + timedelta(days=30))
    assert svc.should_handle_on_expiry(sub) is False


def test_should_handle_true_when_end_date_in_past(monkeypatch):
    """Истёкшая подписка остаётся кандидатом — ветка A/B работает как раньше."""
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    sub = _make_subscription(end_date=datetime.now(UTC) - timedelta(days=1))
    assert svc.should_handle_on_expiry(sub) is True


def test_should_handle_true_when_already_disabled_and_past(monkeypatch):
    """Уже обработанная (expire_disabled_squads непуст) с прошлым end_date — ре-пуш разрешён."""
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    sub = _make_subscription(
        connected_squads=[],
        expire_disabled_squads=['sq-eu'],
        end_date=datetime.now(UTC) - timedelta(days=1),
    )
    assert svc.should_handle_on_expiry(sub) is True


def test_should_handle_true_when_end_date_none(monkeypatch):
    """end_date=None (редкие данные) — поведение не меняем, кандидат как раньше."""
    monkeypatch.setattr(svc, 'is_enabled', lambda: True)
    sub = _make_subscription(end_date=None)
    assert svc.should_handle_on_expiry(sub) is True


# ---------- Гард №2: _handle_user_expired ----------


def _fake_db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _service() -> RemnaWaveWebhookService:
    svc = RemnaWaveWebhookService(MagicMock())
    svc._notify_user = AsyncMock()
    svc._get_renew_keyboard = MagicMock(return_value=None)
    return svc


def _live_subscription() -> MagicMock:
    """Подписка ПОСЛЕ продления: сквады восстановлены, маркеры очищены, статус ACTIVE."""
    sub = MagicMock()
    sub.id = 42
    sub.user_id = 1
    sub.remnawave_id = 12345
    sub.connected_squads = ['sq-eu', 'sq-lte']
    sub.expire_disabled_squads = []
    sub.expire_free_until = None
    sub.status = 'active'
    sub.end_date = datetime.now(UTC) + timedelta(days=30)
    return sub


async def test_user_expired_stale_retry_after_renewal_keeps_squads():
    """Запоздалый user.expired после продления НЕ снимает восстановленные сквады.

    До фикса: _handle_expire_squads вызывается (should_handle_on_expiry → True по непустым
    connected_squads), handle_expiration ставит connected_squads=[], expire_disabled_squads=[...],
    статус EXPIRED — сквады «не возвращаются»."""
    svc = _service()
    sub = _live_subscription()
    svc._handle_expire_squads = AsyncMock(return_value=True)

    tariff = SimpleNamespace(is_daily=False)
    with patch(
        'app.services.remnawave_webhook_service.sa_inspect', return_value=SimpleNamespace(dict={'tariff': tariff})
    ):
        await svc._handle_user_expired(_fake_db(), SimpleNamespace(id=1), sub, {'status': 'EXPIRED'})

    # Ни обработка expire-squads, ни штатный EXPIRED не должны тронуть подписку.
    svc._handle_expire_squads.assert_not_awaited()
    assert sub.connected_squads == ['sq-eu', 'sq-lte']
    assert sub.expire_disabled_squads == []
    assert sub.status == 'active'


async def test_user_expired_still_handles_real_expiry():
    """Реальное истечение (end_date в прошлом) обрабатывается как раньше."""
    svc = _service()
    sub = _live_subscription()
    sub.end_date = datetime.now(UTC) - timedelta(days=1)
    handled = AsyncMock(return_value=True)
    svc._handle_expire_squads = handled

    tariff = SimpleNamespace(is_daily=False)
    with patch(
        'app.services.remnawave_webhook_service.sa_inspect', return_value=SimpleNamespace(dict={'tariff': tariff})
    ):
        await svc._handle_user_expired(_fake_db(), SimpleNamespace(id=1), sub, {'status': 'EXPIRED'})

    handled.assert_awaited_once()


# ---------- Гард №3: внешняя реактивация (user.modified ACTIVE) ----------


def _disabled_subscription(**overrides) -> MagicMock:
    """Подписка ПОСЛЕ ветки A: сквады сняты, маркер отложенных непуст, срок в прошлом."""
    defaults = dict(
        id=42,
        user_id=1,
        status='expired',
        end_date=datetime.now(UTC) - timedelta(days=1),
        is_daily_paused=False,
        connected_squads=[],
        expire_disabled_squads=['sq-eu', 'sq-lte'],
        expire_free_until=None,
        traffic_used_gb=0.0,
        traffic_limit_gb=100,
    )
    defaults.update(overrides)
    return MagicMock(**defaults)


async def test_user_modified_external_reactivation_restores_squads():
    """Внешнее продление (в панели, минуя бота): user.modified(ACTIVE, будущий expireAt)
    должен вернуть отложенные фичей сквады — restore_squads вызывается с reason reactivation.

    До фикса: статус синкался в ACTIVE, а expire_disabled_squads оставался непустым —
    следующий пуш в панель отправил бы пустой connected_squads и сквады «не возвращались»."""
    svc = _service()
    sub = _disabled_subscription()
    restore = AsyncMock(return_value=True)
    future_iso = (datetime.now(UTC) + timedelta(days=30)).isoformat()

    with (
        patch(
            'app.services.remnawave_webhook_service.get_open_grace_subscription_ids',
            AsyncMock(return_value=set()),
        ),
        patch('app.services.remnawave_webhook_service.expire_squad_service.restore_squads', restore),
    ):
        await svc._handle_user_modified(
            _fake_db(), SimpleNamespace(id=1), sub, {'status': 'ACTIVE', 'expireAt': future_iso}
        )

    restore.assert_awaited_once()
    assert restore.await_args.kwargs.get('reason') == 'webhook_external_reactivation'
    assert sub.status == 'active'


async def test_user_modified_external_reactivation_skips_when_no_marker():
    """Подписка без отложенных сквадов (уже восстановлена ботом) не перевосстанавливается."""
    svc = _service()
    sub = _disabled_subscription(expire_disabled_squads=[])
    restore = AsyncMock(return_value=True)
    future_iso = (datetime.now(UTC) + timedelta(days=30)).isoformat()

    with (
        patch(
            'app.services.remnawave_webhook_service.get_open_grace_subscription_ids',
            AsyncMock(return_value=set()),
        ),
        patch('app.services.remnawave_webhook_service.expire_squad_service.restore_squads', restore),
    ):
        await svc._handle_user_modified(
            _fake_db(), SimpleNamespace(id=1), sub, {'status': 'ACTIVE', 'expireAt': future_iso}
        )

    restore.assert_not_awaited()


async def test_user_modified_external_reactivation_skips_free_window():
    """Активное free-окно (ветка B, expire_free_until не очищен) не трогаем —
    restore_squads не вызывается, сквадами управляет сама фича."""
    svc = _service()
    sub = _disabled_subscription(expire_free_until=datetime.now(UTC) + timedelta(days=3))
    restore = AsyncMock(return_value=True)
    future_iso = (datetime.now(UTC) + timedelta(days=30)).isoformat()

    with (
        patch(
            'app.services.remnawave_webhook_service.get_open_grace_subscription_ids',
            AsyncMock(return_value=set()),
        ),
        patch('app.services.remnawave_webhook_service.expire_squad_service.restore_squads', restore),
    ):
        await svc._handle_user_modified(
            _fake_db(), SimpleNamespace(id=1), sub, {'status': 'ACTIVE', 'expireAt': future_iso}
        )

    restore.assert_not_awaited()
