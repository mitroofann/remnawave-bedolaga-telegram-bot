"""[Форк] Фича «гашение сквада при исчерпании трафика» (CUSTOM_TRAFFIC_LIMIT_SQUAD_ENABLED).

Изолированная фича форка (см. память deferred-squad-limit-feature). Вместо перевода подписки
в LIMITED при исчерпании трафика — гасим настроенные в тарифе сквады (`Tariff.limit_disabled_squads`),
оставляя остальные (безлимитные) серверы работать, а подписку — в статусе ACTIVE.

Ключевые идеи:
- RemnaWave управляет серверами per-user на уровне СКВАДОВ (`activeInternalSquads`), не инбаундов.
  Метрируемый инбаунд = отдельный сквад. Гасим сквад целиком.
- Лимит трафика в RemnaWave — на юзера. Чтобы панель не выбила юзера повторно после снятия
  LIMITED, поднимаем панельный лимит до `used + буфер` и ХРАНИМ его точно в
  `subscription.traffic_limit_panel_bytes`. `subscription.traffic_limit_gb` НЕ трогаем —
  в боте юзер видит тарифный лимит (полоска «под завязку»).
- Пока `subscription.traffic_limit_disabled_squads` непусто — все места, пушащие лимит на
  панель, должны брать его через `panel_traffic_limit_bytes()`, иначе вернут тарифный лимит
  и зациклят LIMITED (гарды в monitoring_service / subscription_service / _handle_user_modified).

Восстановление (сквады + панельный лимит) вызывается из точек: ресет трафика по обнулению
тарифа, докупка трафика, ручной сброс, смена/продление тарифа, автооплата, админ-добавление.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Subscription


logger = structlog.get_logger(__name__)

# Буфер сверх потраченного трафика при поднятии панельного лимита (байт).
# Пользователь просил «used + 10КБ»: лимит в боте остаётся тарифным, а на панели держим
# минимальный запас, чтобы юзер не вылетел в LIMITED сразу после снятия.
_LIMIT_BUFFER_BYTES = 10 * 1024

_BYTES_PER_GB = 1024 * 1024 * 1024


def is_enabled() -> bool:
    """Фича включена глобально (тумблер в «Управление ботом»)."""
    return bool(getattr(settings, 'CUSTOM_TRAFFIC_LIMIT_SQUAD_ENABLED', False))


def has_disabled_squads(subscription: Subscription) -> bool:
    """У подписки есть сквады, снятые по лимиту трафика (фича активна для неё)."""
    return bool(subscription.traffic_limit_disabled_squads)


def panel_traffic_limit_bytes(subscription: Subscription) -> int:
    """Лимит трафика (байт), который нужно пушить на панель для этой подписки.

    Пока сквады погашены по лимиту — возвращает сохранённый поднятый лимит (used+буфер),
    иначе тарифный лимит из `subscription.traffic_limit_gb` (0 = безлимит). Используется
    во всех местах, которые отправляют `trafficLimitBytes` на панель, чтобы не сбросить
    поднятый лимит и не зациклить LIMITED.
    """
    if has_disabled_squads(subscription) and subscription.traffic_limit_panel_bytes:
        return int(subscription.traffic_limit_panel_bytes)
    gb = subscription.traffic_limit_gb or 0
    return int(gb) * _BYTES_PER_GB


def _tariff_limit_squads(subscription: Subscription) -> list[str]:
    """UUID сквадов, которые тариф подписки помечает как «гасить при лимите»."""
    tariff = getattr(subscription, 'tariff', None)
    if tariff is None:
        return []
    return list(getattr(tariff, 'limit_disabled_squads', None) or [])


def squads_to_disable(subscription: Subscription) -> list[str]:
    """Какие из подключённых сейчас сквадов нужно погасить при лимите.

    Пересечение настроенных в тарифе лимит-сквадов с реально подключёнными сейчас
    (`connected_squads`), с сохранением порядка connected_squads. Пустой список = гасить
    нечего (тариф не настроен, или лимит-сквады уже не подключены).
    """
    limit_squads = set(_tariff_limit_squads(subscription))
    if not limit_squads:
        return []
    connected = subscription.connected_squads or []
    return [uuid for uuid in connected if uuid in limit_squads]


def _resolve_panel_uuid(subscription: Subscription, user) -> str | None:
    """UUID пользователя в панели: подписочный в multi-tariff, иначе юзерский."""
    if settings.is_multi_tariff_enabled():
        return subscription.remnawave_uuid
    return getattr(user, 'remnawave_uuid', None)


async def disable_squads_on_limit(
    db: AsyncSession,
    user,
    subscription: Subscription,
    *,
    used_bytes: int | None = None,
) -> bool:
    """Погасить лимит-сквады подписки вместо перевода в LIMITED.

    Переносит настроенные в тарифе сквады из ``connected_squads`` в
    ``traffic_limit_disabled_squads``, поднимает панельный лимит до ``used + буфер``
    (сохраняя его точно в ``traffic_limit_panel_bytes``), пушит на панель урезанный
    список сквадов + поднятый лимит + статус ACTIVE и снимает LIMITED (``enable``).
    ``subscription.status`` остаётся ACTIVE, ``traffic_limit_gb`` не меняется.

    Возвращает True, если сквады были погашены; False — если гасить нечего (тогда
    вызывающий должен обработать лимит штатно, т.е. перевести в LIMITED).
    """
    if not is_enabled():
        return False

    # Идемпотентность: если уже погашены (повторный вебхук) — сквады уже перенесены из
    # connected в disabled, поэтому squads_to_disable вернёт пусто. Проверяем ПЕРВОЙ и
    # сообщаем True, чтобы вызывающий не переводил в LIMITED.
    if has_disabled_squads(subscription):
        return True

    target = squads_to_disable(subscription)
    if not target:
        return False

    panel_uuid = _resolve_panel_uuid(subscription, user)
    if not panel_uuid:
        logger.warning(
            'traffic-limit-squad: нет panel uuid, гашение пропущено',
            subscription_id=subscription.id,
            user_id=subscription.user_id,
        )
        return False

    target_set = set(target)
    remaining = [uuid for uuid in (subscription.connected_squads or []) if uuid not in target_set]

    resolved_used = int(used_bytes) if used_bytes is not None else await _fetch_used_bytes(panel_uuid)
    panel_limit = max(resolved_used, 0) + _LIMIT_BUFFER_BYTES

    subscription.connected_squads = remaining
    subscription.traffic_limit_disabled_squads = target
    subscription.traffic_limit_panel_bytes = panel_limit
    await db.commit()
    await db.refresh(subscription)

    ok = await _push_to_panel(db, subscription, panel_uuid, enable=True)
    if not ok:
        logger.error(
            'traffic-limit-squad: не удалось применить гашение на панели',
            subscription_id=subscription.id,
            disabled_squads=target,
        )
    else:
        logger.info(
            'traffic-limit-squad: сквады погашены по лимиту трафика',
            subscription_id=subscription.id,
            user_id=subscription.user_id,
            disabled_squads=target,
            remaining_squads=remaining,
            panel_limit_bytes=panel_limit,
        )
    return True


def apply_restore_fields(subscription: Subscription) -> bool:
    """Чистая (без I/O) мутация полей подписки при восстановлении.

    Возвращает погашенные сквады в ``connected_squads`` (сохраняя порядок, без дублей),
    очищает маркер и поднятый панельный лимит. Коммит и синхронизацию с панелью делает
    вызывающий. Используется:
    - централизованно в ``update_remnawave_user(reset_traffic=True)`` — reset-флоу
      (покупка/продление/смена тарифа/сброс) уже сами пушат сквады+лимит;
    - в ``restore_squads`` — точки без собственного пуша (докупка, вебхук).

    Возвращает True, если было что восстанавливать (маркер был непуст).
    """
    if not has_disabled_squads(subscription):
        return False

    disabled = list(subscription.traffic_limit_disabled_squads or [])
    connected = list(subscription.connected_squads or [])
    merged = connected + [uuid for uuid in disabled if uuid not in connected]

    subscription.connected_squads = merged
    subscription.traffic_limit_disabled_squads = []
    subscription.traffic_limit_panel_bytes = None
    return True


async def restore_squads(db: AsyncSession, subscription: Subscription, *, reason: str) -> bool:
    """Вернуть ранее погашенные лимит-сквады и восстановить тарифный лимит трафика.

    Возвращает сквады из ``traffic_limit_disabled_squads`` в ``connected_squads``,
    очищает маркер и поднятый панельный лимит, затем пушит на панель полный список
    сквадов + тарифный лимит (``traffic_limit_gb``). Идемпотентна: если гасить было
    нечего — тихо возвращает False.
    """
    disabled = list(subscription.traffic_limit_disabled_squads or [])
    if not apply_restore_fields(subscription):
        return False

    await db.commit()
    await db.refresh(subscription)

    from app.database.crud.user import get_user_by_id

    user = await get_user_by_id(db, subscription.user_id)
    panel_uuid = _resolve_panel_uuid(subscription, user) if user else None
    ok = False
    if panel_uuid:
        ok = await _push_to_panel(db, subscription, panel_uuid, enable=True)

    logger.info(
        'traffic-limit-squad: сквады восстановлены',
        subscription_id=subscription.id,
        user_id=subscription.user_id,
        restored_squads=disabled,
        reason=reason,
        panel_ok=ok,
    )
    return True


async def _fetch_used_bytes(panel_uuid: str) -> int:
    """Прочитать текущий использованный трафик юзера с панели (fallback, если не в вебхуке)."""
    try:
        from app.services.subscription_service import SubscriptionService

        service = SubscriptionService()
        async with service.get_api_client() as api:
            panel_user = await api.get_user_by_uuid(panel_uuid)
        if panel_user:
            return int(panel_user.used_traffic_bytes or 0)
    except Exception as error:  # noqa: BLE001
        logger.warning('traffic-limit-squad: не удалось прочитать used_traffic с панели', error=str(error))
    return 0


async def _push_to_panel(db: AsyncSession, subscription: Subscription, panel_uuid: str, *, enable: bool) -> bool:
    """Синхронизировать сквады + лимит подписки с панелью и (опц.) снять LIMITED.

    Переиспользует ``SubscriptionService.update_remnawave_user`` (grace-safe,
    multi-tariff-aware). Он пушит ``connected_squads`` при ``sync_squads=True`` и берёт
    лимит через ``panel_traffic_limit_bytes`` (гард внутри update_remnawave_user).
    PATCH может не снять статус LIMITED — поэтому дополнительно вызываем enable.
    """
    try:
        from app.services.subscription_service import SubscriptionService

        service = SubscriptionService()
        updated = await service.update_remnawave_user(
            db,
            subscription,
            reset_traffic=False,
            sync_squads=True,
        )
        if enable and panel_uuid:
            await service.enable_remnawave_user(panel_uuid, db=db)
        return updated is not None
    except Exception as error:  # noqa: BLE001
        logger.error('traffic-limit-squad: ошибка синхронизации с панелью', error=str(error))
        return False
