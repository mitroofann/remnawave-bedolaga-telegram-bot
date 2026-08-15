"""[Форк] Фича «уведомления о несвежей подписке» (NOTIFY_STALE_SUB_ENABLED).

Раз в сутки (настраиваемое время HH:MM) проверяем активных пользователей на
устройства, которые были онлайн НЕДАВНО (lastSeen устройства < N часов), но ДАВНО
не запрашивали актуальный список серверов (последний запрос подписки по userAgent
> M часов). Таким пользователям шлём уведомление «обновите подписку вручную».

Ключевые инварианты:
- Логика СТРОГО пер-устройства: уведомление про устройство шлём только если именно
  это устройство недавно было онлайн. «Давно не был онлайн → логично не обновлялся»
  — таких не тревожим.
- Маппинг «запрос подписки → устройство» — по userAgent (устройства не подписаны
  своим ID в history): ищем записи, где userAgent совпадает с userAgent устройства
  по подстроке (case-insensitive, в обе стороны).
- Если проблемных устройств несколько — ОДНО уведомление с упоминанием всех.
- Рекомендация Happ/INCY добавляется только если НИ в одном проблемном устройстве
  нет подстроки 'happ'/'incy' (case-insensitive).
- Повторная отправка — только по интервалу (NOTIFY_STALE_SUB_COOLDOWN_DAYS): проверяем
  последнюю запись sent_notifications с типом 'stale_sub'; запись «устаревает сама»
  по created_at (сравнение с now - cooldown), чистка не нужна.

I/O отделён от чистой логики: функции без аннотаций ``async`` не делают I/O и
легко тестируются. Сбор кандидатов — через RemnaWave API (get_user_devices_all +
get_subscription_request_history); отправку выполняет MonitoringService.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import Subscription, User, _aware
from app.services.subscription_service import SubscriptionService


# Алиас-тип для isinstance: имя datetime в модуле тесты могут патчить
# (patch('...stale_sub_service.datetime')), а isinstance требует именно класс.
_DATETIME_TYPE = datetime


logger = structlog.get_logger(__name__)


def is_enabled() -> bool:
    """Фича включена глобально (тумблер в «Уведомления пользователям»)."""
    return bool(getattr(settings, 'NOTIFY_STALE_SUB_ENABLED', False))


# =================================================================================
# Чистая логика (без I/O) — покрыта юнит-тестами
# =================================================================================


def _parse_datetime_or_none(value: Any) -> datetime | None:
    """Распарсить ISO-строку времени в aware-datetime; мусор → None."""
    if value is None:
        return None
    try:
        return _aware(datetime.fromisoformat(str(value).replace('Z', '+00:00')))
    except (TypeError, ValueError):
        return None


def _check_time_due(now: datetime, check_time: time | None) -> bool:
    """Наступило ли время суточной проверки (HH:MM) в момент ``now``.

    Сравнение только по часам/минутам — дневной цикл тикает раз в минуту и
    срабатывает в первой минуте после наступления заданного времени.
    """
    if check_time is None:
        return False
    now = _aware(now)
    return (now.hour, now.minute) == (check_time.hour, check_time.minute)


def _matches_app_recommendation(device_names: list[str]) -> bool:
    """Добавлять рекомендацию Happ/INCY?

    True — только если НИ в одном из проблемных устройств нет подстроки 'happ'/'incy'
    (case-insensitive). Пользователь уже на Happ/INCY → рекомендация не нужна.
    """
    for name in device_names:
        lowered = str(name).lower()
        if 'happ' in lowered or 'incy' in lowered:
            return False
    return True


def _join_device_names(device_names: list[str]) -> str:
    """Список имён устройств для подстановки в текст (без HTML-экранирования)."""
    return ', '.join(str(name) for name in device_names if name)


@dataclass
class StaleDevice:
    """Проблемное устройство: активно используется, но подписка не обновлялась."""

    name: str
    hwid: str
    last_seen: datetime | None
    last_request: datetime | None


def _device_last_seen(device: dict[str, Any]) -> datetime | None:
    """Время последнего подключения устройства: lastSeen (fallback updatedAt)."""
    return _parse_datetime_or_none(device.get('lastSeen') or device.get('updatedAt'))


def _device_ua_matches_history(device_ua: str | None, record_ua: str | None) -> bool:
    """Совпадает ли userAgent устройства с userAgent записи истории (подстрока, ci)."""
    if not device_ua or not record_ua:
        return False
    device_ua = device_ua.lower()
    record_ua = record_ua.lower()
    return device_ua in record_ua or record_ua in device_ua


def classify_device(
    device: dict[str, Any],
    history_ua_map: dict[str, datetime],
    *,
    now: datetime,
    last_seen_hours: int,
    last_request_hours: int,
) -> StaleDevice | None:
    """Оценить одно устройство: кандидат на уведомление?

    Условия (оба обязательны):
    1. Устройство было онлайн НЕДАВНО: lastSeen >= now - last_seen_hours.
    2. Последний запрос подписки (по userAgent) ДАВНО: <= now - last_request_hours.

    last_seen_hours <= 0 или last_request_hours <= 0 трактуем как «порог выключен»
    (условие не проверяется) — удобно для smoke-теста фичи.
    """
    last_seen = _device_last_seen(device)
    if last_seen is None:
        return None

    now = _aware(now)
    last_seen = _aware(last_seen)

    if last_seen_hours > 0 and last_seen < now - timedelta(hours=last_seen_hours):
        return None  # устройство давно не было онлайн — не тревожим

    device_ua = device.get('userAgent')
    last_request = None
    if device_ua:
        # Ищем запись истории с совпадающим userAgent (и по подстроке в обе стороны).
        for record_ua, record_at in history_ua_map.items():
            if _device_ua_matches_history(device_ua, record_ua):
                if last_request is None or record_at > last_request:
                    last_request = record_at

    if last_request is None:
        return None  # нет истории по userAgent — не знаем, обновлялась ли
    last_request = _aware(last_request)

    if last_request_hours > 0 and last_request >= now - timedelta(hours=last_request_hours):
        return None  # подписка недавно запрашивалась — всё в порядке

    name = device.get('deviceModel') or device.get('model') or device.get('name') or 'Unknown'
    return StaleDevice(
        name=str(name),
        hwid=str(device.get('hwid') or device.get('deviceId') or device.get('id') or ''),
        last_seen=last_seen,
        last_request=last_request,
    )


def filter_candidates(
    devices: list[dict[str, Any]],
    history_ua_map: dict[str, datetime],
    *,
    now: datetime,
    last_seen_hours: int,
    last_request_hours: int,
    recommend: bool,
) -> list[StaleDevice]:
    """Отобрать проблемные устройства пользователя.

    ``recommend=False`` — не тревожить Happ/INCY-устройства (рекомендация им не нужна,
    а уведомление ни к чему). Сортировка по last_seen (свежие сверху).
    """
    candidates: list[StaleDevice] = []
    for device in devices:
        stale = classify_device(
            device,
            history_ua_map,
            now=now,
            last_seen_hours=last_seen_hours,
            last_request_hours=last_request_hours,
        )
        if stale is None:
            continue
        if not recommend and not _matches_app_recommendation([stale.name]):
            continue
        candidates.append(stale)

    candidates.sort(key=lambda d: d.last_seen or datetime.min, reverse=True)
    return candidates


def should_skip_repeat(
    last_notification_row,
    *,
    now: datetime,
    cooldown_days: int,
) -> bool:
    """Пропустить ли повторную отправку по cooldown?

    Если последняя запись 'stale_sub' для (user, subscription) свежее
    ``now - cooldown_days`` — уведомление уже слали недавно, пропускаем.
    """
    if last_notification_row is None:
        return False
    created_at = getattr(last_notification_row, 'created_at', None)
    if not isinstance(created_at, _DATETIME_TYPE):
        return False
    created_at = _aware(created_at)
    return created_at > _aware(now) - timedelta(days=cooldown_days)


def build_message(device_names: list[str], recommend: bool, *, texts) -> str:
    """Собрать текст уведомления: single/multi + опциональная рекомендация.

    ``texts`` — объект Texts (get_texts(user.language)); ключи отсутствующие в языке
    подставляются дефолтом (фоллбэк ru в loader'е, но держим и локальный дефолт).
    """
    joined = _join_device_names(device_names)
    if len(device_names) > 1:
        message = texts.t(
            'STALE_SUB_MULTI',
            '⚠️ <b>Обновите подписку</b>\n\n'
            'Мы заметили, что ваши устройства {devices} давно не запрашивали актуальный '
            'список серверов — советуем обновить их вручную, чтобы всё стабильно работало '
            '(в приложении нажмите на кнопку обновления справа от названия Bulka VPN).',
        ).format(devices=joined)
    else:
        message = texts.t(
            'STALE_SUB_SINGLE',
            '⚠️ <b>Обновите подписку</b>\n\n'
            'Мы заметили, что ваше устройство {devices} давно не запрашивало актуальный '
            'список серверов — советуем обновить вручную, чтобы всё стабильно работало '
            '(в приложении нажмите на кнопку обновления справа от названия Bulka VPN).',
        ).format(devices=joined)

    if recommend:
        tip = texts.t(
            'STALE_SUB_RECOMMEND',
            '\n\n💡 Также рекомендуем использовать приложения Happ или INCY — они самые надёжные.',
        )
        if tip:
            message += tip
    return message


# =================================================================================
# I/O: сбор кандидатов через RemnaWave API
# =================================================================================


async def collect_candidates(db, *, now: datetime) -> list[tuple[User, Subscription, list[StaleDevice]]]:
    """Собрать (user, subscription, проблемные устройства) по всем активным подпискам.

    Возвращает только подписки, у которых есть хотя бы одно проблемное устройство.
    ``db`` — AsyncSession (для чтения активных подписок), ``now`` — момент проверки.
    """
    from app.database.models import SubscriptionStatus, UserStatus

    result = await db.execute(
        select(Subscription)
        .join(User, Subscription.user_id == User.id)
        .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
        .where(
            Subscription.status.in_([SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value]),
            User.status == UserStatus.ACTIVE.value,
        )
    )
    subscriptions = result.scalars().all()

    now = _aware(now)
    last_seen_hours = int(getattr(settings, 'NOTIFY_STALE_SUB_LAST_SEEN_HOURS', 24) or 0)
    last_request_hours = int(getattr(settings, 'NOTIFY_STALE_SUB_LAST_REQUEST_HOURS', 72) or 0)
    recommend = bool(getattr(settings, 'NOTIFY_STALE_SUB_RECOMMEND_APPS', True))

    candidates: list[tuple[User, Subscription, list[StaleDevice]]] = []
    for subscription in subscriptions:
        user = subscription.user
        if not user:
            continue
        panel_user_id = _resolve_panel_user_id(subscription, user)
        if not panel_user_id:
            continue

        try:
            service = SubscriptionService()
            async with service.get_api_client() as api:
                devices = await _fetch_devices(api, panel_user_id)
                history_ua_map = await _build_history_ua_map(api, panel_user_id)

            stale_devices = filter_candidates(
                devices,
                history_ua_map,
                now=now,
                last_seen_hours=last_seen_hours,
                last_request_hours=last_request_hours,
                recommend=recommend,
            )
            if stale_devices:
                candidates.append((user, subscription, stale_devices))
        except Exception as error:
            logger.error(
                'stale-sub: ошибка сбора кандидатов',
                subscription_id=subscription.id,
                user_id=subscription.user_id,
                error=str(error),
            )
    return candidates


async def _fetch_devices(api, panel_user_id: int) -> list[dict[str, Any]]:
    """Все устройства пользователя панели (get_user_devices_all)."""
    data = await api.get_user_devices_all(panel_user_id)
    return list(data.get('devices', []) or [])


async def _build_history_ua_map(api, panel_user_id: int) -> dict[str, datetime]:
    """userAgent → время последнего запроса подписки (из history).

    По каждому userAgent в истории берём максимум requestAt. Сопоставление с
    устройством — по подстроке userAgent (см. _device_ua_matches_history).
    """
    data = await api.get_subscription_request_history(panel_user_id)
    records = data.get('records', []) or []
    ua_map: dict[str, datetime] = {}
    for record in records:
        record_ua = record.get('userAgent')
        if not record_ua:
            continue
        record_at = _parse_datetime_or_none(record.get('requestAt'))
        if record_at is None:
            continue
        prev = ua_map.get(str(record_ua))
        if prev is None or record_at > prev:
            ua_map[str(record_ua)] = record_at
    return ua_map


def _resolve_panel_user_id(subscription: Subscription, user) -> int | None:
    """Числовой id пользователя в панели (Remnawave 3.0.0): подписочный в multi-tariff,
    иначе юзерский. UUID больше не адресует пользователя — панель ждёт число."""
    if settings.is_multi_tariff_enabled():
        return subscription.remnawave_id
    return getattr(user, 'remnawave_id', None)
