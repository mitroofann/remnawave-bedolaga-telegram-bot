"""Remnawave 2.8.0 merged the 4 per-interval expiration webhooks into a single
``user.expiration`` event carrying ``meta.expiration`` (signed hours). The bot
must handle the new event (or expiration notifications silently stop on 2.8.0),
while still accepting the old events from 2.7.x panels.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.services.remnawave_webhook_service import RemnaWaveWebhookService


def _service() -> RemnaWaveWebhookService:
    svc = RemnaWaveWebhookService(MagicMock())
    svc._notify_user = AsyncMock()
    svc._get_renew_keyboard = MagicMock(return_value=None)
    return svc


def _user() -> MagicMock:
    u = MagicMock()
    u.id = 1
    return u


def _sub() -> MagicMock:
    s = MagicMock()
    s.id = 42
    return s


def _sent_key(svc: RemnaWaveWebhookService) -> str:
    # _notify_user(user, text_key, *, ...)
    return svc._notify_user.await_args.args[1]


def _receiver_data(expiration) -> dict:
    """Build the handler ``data`` exactly as the webhook receiver does.

    The receiver injects the envelope ``meta`` under ``data['_meta']`` (see
    app/webserver/remnawave_webhook.py: "Inject meta into data so handlers can
    access it via data.get('_meta')"). Handlers read ``_meta`` — NOT ``meta``.
    Tests MUST go through this contract, otherwise they silently pass against
    the wrong key and mask a production break (every notification lost).
    """
    payload = {'event': 'user.expiration', 'meta': {'expiration': expiration}}
    data: dict = {}
    meta = payload.get('meta')
    if isinstance(meta, dict):
        data['_meta'] = meta
    return data


async def test_new_and_old_events_both_registered():
    svc = _service()
    # 2.8.0 event handled...
    assert svc._user_handlers.get('user.expiration') is not None
    # ...and 2.7.x events kept for backward compatibility.
    for old in (
        'user.expires_in_72_hours',
        'user.expires_in_48_hours',
        'user.expires_in_24_hours',
        'user.expired_24_hours_ago',
    ):
        assert old in svc._user_handlers


async def test_canonical_hours_map_to_legacy_messages():
    cases = {
        -72: 'WEBHOOK_SUB_EXPIRES_72H',
        -48: 'WEBHOOK_SUB_EXPIRES_48H',
        -24: 'WEBHOOK_SUB_EXPIRES_24H',
        24: 'WEBHOOK_SUB_EXPIRED_24H_AGO',
    }
    for hours, expected in cases.items():
        svc = _service()
        await svc._handle_user_expiration(None, _user(), _sub(), _receiver_data(hours))
        svc._notify_user.assert_awaited_once()
        assert _sent_key(svc) == expected


async def test_reads_receiver_meta_key_not_raw_meta():
    """Regression: the handler reads data['_meta'] (receiver contract). A payload
    carrying the RAW envelope key 'meta' (i.e. receiver injection skipped) must
    NOT produce a notification — this is exactly the bug that silently dropped
    every user.expiration notification on 2.8.0."""
    svc = _service()
    await svc._handle_user_expiration(None, _user(), _sub(), {'meta': {'expiration': -24}})
    svc._notify_user.assert_not_awaited()


async def test_non_canonical_negative_picks_nearest_before_message():
    svc = _service()
    # -30 is closest to -24 → "expires in <24h" message.
    await svc._handle_user_expiration(None, _user(), _sub(), _receiver_data(-30))
    assert _sent_key(svc) == 'WEBHOOK_SUB_EXPIRES_24H'


async def test_non_canonical_positive_uses_expired_message():
    svc = _service()
    await svc._handle_user_expiration(None, _user(), _sub(), _receiver_data(48))
    assert _sent_key(svc) == 'WEBHOOK_SUB_EXPIRED_24H_AGO'


async def test_missing_or_invalid_meta_sends_nothing():
    for data in ({'_meta': {}}, {'_meta': {'expiration': 'oops'}}, {}, {'_meta': None}):
        svc = _service()
        await svc._handle_user_expiration(None, _user(), _sub(), data)
        svc._notify_user.assert_not_awaited()


async def test_no_subscription_sends_nothing():
    svc = _service()
    await svc._handle_user_expiration(None, _user(), None, _receiver_data(-24))
    svc._notify_user.assert_not_awaited()


async def test_new_2_8_0_api_token_admin_events_registered():
    """2.8.0 added service.api_token_created/deleted — surfaced as admin notifications."""
    svc = _service()
    assert 'service.api_token_created' in svc._admin_handlers
    assert 'service.api_token_deleted' in svc._admin_handlers


async def test_user_modified_syncs_used_traffic_from_nested_user_traffic():
    """usedTrafficBytes lives nested in userTraffic (ExtendedUsersSchema); the
    user.modified handler must read it there, else used-traffic never syncs."""
    svc = _service()
    sub = MagicMock()
    sub.status = 'active'
    sub.traffic_used_gb = 0.0
    await svc._handle_user_modified(AsyncMock(), _user(), sub, {'userTraffic': {'usedTrafficBytes': 5 * 1024**3}})
    assert sub.traffic_used_gb == 5.0


async def test_user_modified_used_traffic_falls_back_to_flat_key():
    """Old panels send a flat usedTrafficBytes — keep the fallback working."""
    svc = _service()
    sub = MagicMock()
    sub.status = 'active'
    sub.traffic_used_gb = 0.0
    await svc._handle_user_modified(AsyncMock(), _user(), sub, {'usedTrafficBytes': 2 * 1024**3})
    assert sub.traffic_used_gb == 2.0


# ---------- [Форк] expire-squad через user.modified (status=EXPIRED) ----------


async def test_user_modified_expired_triggers_expire_squads():
    """Панель шлёт истечение как user.modified(status=EXPIRED), НЕ user.expired.
    Хендлер должен делегировать в _handle_expire_squads (ветка A/B) и выйти сразу."""
    from datetime import UTC, datetime, timedelta

    svc = _service()
    svc._handle_expire_squads = AsyncMock(return_value=True)
    sub = MagicMock()
    sub.id = 42
    sub.status = 'active'
    sub.end_date = datetime.now(UTC) - timedelta(days=1)  # реально истекла (срок в прошлом)
    sub.is_daily_paused = False

    with (
        patch(
            'app.services.remnawave_webhook_service.get_open_grace_subscription_ids',
            AsyncMock(return_value=set()),
        ),
        patch('app.services.remnawave_webhook_service.expire_squad_service') as ess,
        patch('app.services.remnawave_webhook_service.sa_inspect') as insp,
    ):
        insp.return_value.dict.get.return_value = None  # tariff → None (не суточная)
        ess.is_free_window_active.return_value = False
        await svc._handle_user_modified(AsyncMock(), _user(), sub, {'status': 'EXPIRED'})

    svc._handle_expire_squads.assert_awaited_once()


async def test_user_modified_stale_expired_retry_after_renewal_is_ignored():
    """Регрессия: панель ретраит EXPIRED-событие ~19 раз со СТАРЫМ снимком (expireAt в прошлом).
    Если бот уже продлил подписку (end_date в будущем, статус ACTIVE), запоздалый EXPIRED-ретрай
    НЕ должен ни откатывать end_date назад, ни снова снимать сквады — иначе продление затирается."""
    from datetime import UTC, datetime, timedelta

    svc = _service()
    svc._handle_expire_squads = AsyncMock(return_value=True)
    sub = MagicMock()
    sub.id = 42
    sub.status = 'active'
    future = datetime.now(UTC) + timedelta(days=1)
    sub.end_date = future
    sub.is_daily_paused = False

    with patch(
        'app.services.remnawave_webhook_service.get_open_grace_subscription_ids',
        AsyncMock(return_value=set()),
    ):
        # Ретрай несёт старый expireAt в прошлом + status=EXPIRED.
        past_iso = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        await svc._handle_user_modified(AsyncMock(), _user(), sub, {'status': 'EXPIRED', 'expireAt': past_iso})

    # Сквады НЕ снимаются повторно, end_date НЕ откачен назад.
    svc._handle_expire_squads.assert_not_awaited()
    assert sub.end_date == future


async def test_user_modified_active_future_does_not_trigger_expire_squads():
    """Обычный user.modified (ACTIVE, срок в будущем) НЕ должен трогать expire-squad."""
    from datetime import UTC, datetime, timedelta

    svc = _service()
    svc._handle_expire_squads = AsyncMock(return_value=True)
    sub = MagicMock()
    sub.id = 42
    sub.status = 'active'
    sub.end_date = datetime.now(UTC) + timedelta(days=10)

    with patch(
        'app.services.remnawave_webhook_service.get_open_grace_subscription_ids',
        AsyncMock(return_value=set()),
    ):
        await svc._handle_user_modified(AsyncMock(), _user(), sub, {'status': 'ACTIVE'})

    svc._handle_expire_squads.assert_not_awaited()


# ---------- [Форк] разрыв петли ре-пушей expire-squad ----------


async def test_handle_expire_squads_skips_repush_on_own_push_echo():
    """Регрессия (шторм ~100 user.modified/сек): подписка уже обработана (expire_disabled_squads
    заполнены) и МЫ САМИ недавно пушили expire-состояние (маркер _recent_expire_pushes) → входящий
    EXPIRED это эхо нашего же push. Не перепушиваем (иначе петля), но возвращаем True, чтобы штатный
    EXPIRED не сработал."""
    from datetime import UTC, datetime

    svc = _service()
    sub = MagicMock()
    sub.id = 42
    # МЫ сами только что пушили expire-состояние → echo-окно открыто.
    svc._recent_expire_pushes = {42: datetime.now(UTC)}

    with patch('app.services.remnawave_webhook_service.expire_squad_service') as ess:
        ess.is_enabled.return_value = True
        ess.has_expire_disabled_squads.return_value = True  # already_handled
        ess.handle_expiration = AsyncMock(return_value=True)

        handled = await svc._handle_expire_squads(AsyncMock(), _user(), sub, {'status': 'EXPIRED'})

    assert handled is True
    ess.handle_expiration.assert_not_awaited()  # ре-пуш НЕ выполнен — петля разорвана


async def test_handle_expire_squads_not_muted_by_routine_sync():
    """КЛЮЧЕВАЯ регрессия ветки B: рутинный user.modified (панельный синк) за <60с до подлинного
    истечения штампует generic last_webhook_update_at, но НЕ наш _recent_expire_pushes. Раньше
    echo-guard читал generic-штамп и молча глушил ветку A/B. Теперь — НЕ глушит: подлинное
    истечение обрабатывается (handle_expiration вызывается)."""
    from datetime import UTC, datetime

    svc = _service()
    sub = MagicMock()
    sub.id = 42
    # Рутинный синк недавно трогал подписку (generic-штамп взведён)...
    sub.last_webhook_update_at = datetime.now(UTC)
    # ...но МЫ сами expire ещё не пушили — маркер пуст.
    svc._recent_expire_pushes = {}

    with patch('app.services.remnawave_webhook_service.expire_squad_service') as ess:
        ess.is_enabled.return_value = True
        ess.has_expire_disabled_squads.return_value = True  # already_handled (сквады отложены)
        ess.resolve_free_squads.return_value = []
        ess.handle_expiration = AsyncMock(return_value=True)

        handled = await svc._handle_expire_squads(AsyncMock(), _user(), sub, {'status': 'EXPIRED'})

    assert handled is True
    ess.handle_expiration.assert_awaited_once()  # НЕ заглушено рутинным синком


async def test_handle_expire_squads_still_repushes_after_echo_window():
    """Вне echo-окна (мы давно не пушили) ре-пуш ВЫПОЛНЯЕТСЯ — фича самолечится."""
    svc = _service()
    sub = MagicMock()
    sub.id = 42
    svc._recent_expire_pushes = {}  # мы не пушили → echo-окно закрыто

    with patch('app.services.remnawave_webhook_service.expire_squad_service') as ess:
        ess.is_enabled.return_value = True
        ess.has_expire_disabled_squads.return_value = True
        ess.resolve_free_squads.return_value = []  # ветка A → без уведомления
        ess.handle_expiration = AsyncMock(return_value=True)

        handled = await svc._handle_expire_squads(AsyncMock(), _user(), sub, {'status': 'EXPIRED'})

    assert handled is True
    ess.handle_expiration.assert_awaited_once()


async def test_handle_expire_squads_stamps_own_push_marker():
    """После успешного нашего push маркер _recent_expire_pushes взводится → следующий эхо-EXPIRED
    внутри окна будет пропущен (петля разорвана именно нашим push, а не любым webhook)."""
    svc = _service()
    sub = MagicMock()
    sub.id = 42
    svc._recent_expire_pushes = {}

    with patch('app.services.remnawave_webhook_service.expire_squad_service') as ess:
        ess.is_enabled.return_value = True
        ess.has_expire_disabled_squads.return_value = True
        ess.resolve_free_squads.return_value = []
        ess.handle_expiration = AsyncMock(return_value=True)

        await svc._handle_expire_squads(AsyncMock(), _user(), sub, {'status': 'EXPIRED'})

    assert 42 in svc._recent_expire_pushes  # наш push отмечен
    assert svc._is_recent_expire_push(42) is True
