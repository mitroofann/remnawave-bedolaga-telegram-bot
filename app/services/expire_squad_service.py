"""[Форк] Фича «снятие сквадов при истечении подписки» (CUSTOM_EXPIRE_CLEAR_SQUADS_ENABLED).

Изолированная фича форка (см. память deferred-squad-limit-feature — парная фича по образцу).
Две ветки:

- **Ветка A (глобальный флаг).** При истечении подписки снимаем у юзера ВСЕ сквады в панели
  (push ``activeInternalSquads=[]``), статус → EXPIRED. Панель начинает показывать корректное
  «подписка истекла» вместо мёртвых хостов. Оригинальные сквады сохраняем в
  ``subscription.expire_disabled_squads`` для восстановления при продлении.
- **Ветка B (пер-тариф, перекрывает A для тарифа).** Если тариф задаёт ``expire_free_squads``
  (непустой список) И ``expire_free_days > 0`` — при истечении вместо снятия всех сквадов ставим
  free-сквад(ы), пушим их + панельный ``expireAt = now + expire_free_days`` + статус ACTIVE.
  Реальный ``subscription.end_date`` НЕ трогаем (он уже в прошлом). ``expire_free_until`` хранит
  панельный expireAt и служит маркером активного free-окна. По истечении N дней free-сквад
  снимается (push []) и статус → EXPIRED (конечное состояние = ветка A).

Ключевые инварианты:
- ``expire_disabled_squads`` непусто ⟺ фича активна для подписки (A или B).
- ``is_free_window_active`` (expire_free_until в будущем) ⟺ ветка B активна СЕЙЧАС; используется
  как гард во ВСЕХ местах, которые иначе флипнули бы ACTIVE+прошедший end_date → EXPIRED и
  затёрли бы наш панельный expireAt/сквады.
- Пуш на панель — ПРЯМОЙ ``api.update_user`` (не ``update_remnawave_user``), т.к. тот пересчитал
  бы статус/expire из is_actually_active и отправил бы DISABLED+прошлый expire, убив free-окно.

Восстановление реальных сквадов — при продлении/смене тарифа (choke-точки в subscription_service
/ crud). Топ-ап трафика НЕ восстанавливает (он не двигает end_date → сразу снова истекло бы).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Subscription, SubscriptionStatus, _aware


# Алиас-тип для isinstance: имя datetime в модуле тесты могут патчить
# (patch('...expire_squad_service.datetime')), а isinstance требует именно класс.
_DATETIME_TYPE = datetime


logger = structlog.get_logger(__name__)


def is_enabled() -> bool:
    """Фича включена глобально (тумблер в «Управление ботом»)."""
    return bool(getattr(settings, 'CUSTOM_EXPIRE_CLEAR_SQUADS_ENABLED', False))


def has_expire_disabled_squads(subscription: Subscription) -> bool:
    """У подписки отложены сквады из-за истечения (фича активна для неё, A или B)."""
    return bool(getattr(subscription, 'expire_disabled_squads', None))


def is_free_window_active(subscription: Subscription, *, now: datetime | None = None) -> bool:
    """Активно ли СЕЙЧАС free-окно (ветка B): панельный expireAt ещё в будущем.

    УНИВЕРСАЛЬНЫЙ гард. True ⟺ ``expire_free_until`` задан и позже текущего момента.
    Пока True — все места, флипающие ACTIVE+прошедший end_date → EXPIRED или синкающие
    end_date из панели, должны сделать короткое замыкание, иначе сломают free-окно.
    """
    until = getattr(subscription, 'expire_free_until', None)
    # Defensive: не-datetime (битые данные, MagicMock в тестах) ≠ активное free-окно.
    if not isinstance(until, _DATETIME_TYPE):
        return False
    until = _aware(until)
    if now is None:
        now = datetime.now(UTC)
    return until > now


def _tariff_free_squads(subscription: Subscription) -> list[str]:
    """UUID free-сквадов, заданных тарифом подписки (ветка B)."""
    tariff = getattr(subscription, 'tariff', None)
    if tariff is None:
        return []
    return list(getattr(tariff, 'expire_free_squads', None) or [])


def _tariff_free_days(subscription: Subscription) -> int:
    """Сколько дней доступен free-сквад по тарифу (0 = ветка B выключена)."""
    tariff = getattr(subscription, 'tariff', None)
    if tariff is None:
        return 0
    try:
        return int(getattr(tariff, 'expire_free_days', 0) or 0)
    except (TypeError, ValueError):
        return 0


def resolve_free_squads(subscription: Subscription) -> list[str]:
    """Free-сквады для ветки B, если она применима к подписке; иначе пустой список.

    Ветка B применима ⟺ тариф задаёт непустой список free-сквадов И положительное число дней.
    Пустой результат означает «работает ветка A» (снять все сквады).
    """
    if _tariff_free_days(subscription) <= 0:
        return []
    return _tariff_free_squads(subscription)


def _resolve_panel_user_id(subscription: Subscription, user) -> int | None:
    """Числовой id пользователя в панели (Remnawave 3.0.0): подписочный в multi-tariff,
    иначе юзерский. UUID больше не адресует пользователя — панель ждёт число."""
    if settings.is_multi_tariff_enabled():
        return subscription.remnawave_id
    return getattr(user, 'remnawave_id', None)


def should_handle_on_expiry(subscription: Subscription) -> bool:
    """Пред-проверка (без I/O) для sync/monitoring: применять фичу при истечении?

    True когда фича включена И (у подписки есть подключённые сквады, которые надо снять |
    фича уже активна для неё — повторно обработать/ре-пушить).
    """
    if not is_enabled():
        return False
    if has_expire_disabled_squads(subscription):
        return True
    # [Форк-фикс] Живая подписка (end_date в будущем) не может «истечь»: предикат вызывается
    # и с УСТАРЕВШИМИ снапшотами (запоздалый вебхук user.expired / user.modified(EXPIRED)
    # после продления, когда сквады уже восстановлены, а маркер expire_disabled_squads очищен).
    # Снимать только что возвращённые продлением сквады нельзя — юзер видит «при продлении
    # сквады не возвращаются». Уже-обработанные (expire_disabled_squads непуст) отсекаются выше
    # и ре-пушат текущее состояние как раньше; ветка B (free-окно) имеет end_date в прошлом —
    # гард её не трогает.
    end_date = getattr(subscription, 'end_date', None)
    if end_date is not None and _aware(end_date) > datetime.now(UTC):
        return False
    # [Форк-хардн] getattr с дефолтом: предикат зовётся из _check_expired_subscriptions,
    # куда тесты (и сторонние объекты) могут передать подписку без полей (напр. голый
    # SimpleNamespace без connected_squads) — не падаем, считаем «снимать нечего».
    return bool(getattr(subscription, 'connected_squads', None))


async def handle_expiration(db: AsyncSession, user, subscription: Subscription) -> bool:
    """Обработать истечение подписки: снять сквады (A) либо выдать free-сквад (B).

    Идемпотентна: повторный вызов на уже обработанной подписке ПЕРЕПУШИВАЕТ текущее состояние
    (полезно, если панель догоняет или пуш ранее не прошёл). Возвращает True, если фича
    применена (вызывающий не должен делать штатное «просто отключить доступ» поверх).
    Возвращает False, если фича выключена или гасить/выдавать нечего.
    """
    if not is_enabled():
        return False

    panel_user_id = _resolve_panel_user_id(subscription, user)
    now = datetime.now(UTC)

    # [Форк-фикс] Подписка всё ещё оплачена (end_date в будущем) — это НЕ истечение:
    # free-окно/снятие сквадов ей не положено. Защита от «хвоста»: до фикса free-окно
    # могло быть выдано живой подписке (панель получала будущий expireAт, а значит срок
    # подписки «подменялся»), а при завершении окна доступ отрубался, хотя человек
    # оплатил до конца. Патч-утечки: data-driven вызовы (вебхук/сканер) передают
    # правильную «истёкшую» подписку — их это не задевает.
    end_date = getattr(subscription, 'end_date', None)
    if end_date is not None and _aware(end_date) > now:
        return False

    free_squads = resolve_free_squads(subscription)

    # Идемпотентность: фича уже активна для подписки — просто перепушиваем текущее состояние.
    if has_expire_disabled_squads(subscription):
        if panel_user_id:
            if is_free_window_active(subscription, now=now):
                await _push_to_panel(
                    subscription,
                    panel_user_id,
                    active_squads=subscription.connected_squads or [],
                    expire_at=_aware(subscription.expire_free_until),
                    status=SubscriptionStatus.ACTIVE,
                )
            else:
                await _push_to_panel(
                    subscription,
                    panel_user_id,
                    active_squads=[],
                    expire_at=_aware(subscription.end_date),
                    status=SubscriptionStatus.EXPIRED,
                )
        return True

    original = list(getattr(subscription, 'connected_squads', None) or [])
    if not original and not free_squads:
        # Сквадов нет и выдавать нечего — фича не применима, пусть штатный путь отработает.
        return False

    if not panel_user_id:
        logger.warning(
            'expire-squad: нет panel id, обработка пропущена',
            subscription_id=subscription.id,
            user_id=subscription.user_id,
        )
        return False

    subscription.expire_disabled_squads = original

    if free_squads:
        # Ветка B: выдаём free-сквад(ы), держим статус ACTIVE панельно на N дней.
        free_days = _tariff_free_days(subscription)
        panel_until = now + timedelta(days=free_days)
        subscription.connected_squads = list(free_squads)
        subscription.expire_free_until = panel_until
        subscription.status = SubscriptionStatus.ACTIVE.value
        await db.commit()
        await db.refresh(subscription)

        ok = await _push_to_panel(
            subscription,
            panel_user_id,
            active_squads=list(free_squads),
            expire_at=panel_until,
            status=SubscriptionStatus.ACTIVE,
        )
        logger.info(
            'expire-squad: выдан free-сквад при истечении (ветка B)',
            subscription_id=subscription.id,
            user_id=subscription.user_id,
            free_squads=free_squads,
            free_days=free_days,
            panel_until=panel_until.isoformat(),
            panel_ok=ok,
        )
    else:
        # Ветка A: снимаем все сквады, статус EXPIRED.
        subscription.connected_squads = []
        subscription.expire_free_until = None
        subscription.status = SubscriptionStatus.EXPIRED.value
        await db.commit()
        await db.refresh(subscription)

        ok = await _push_to_panel(
            subscription,
            panel_user_id,
            active_squads=[],
            expire_at=_aware(subscription.end_date),
            status=SubscriptionStatus.EXPIRED,
        )
        logger.info(
            'expire-squad: сняты все сквады при истечении (ветка A)',
            subscription_id=subscription.id,
            user_id=subscription.user_id,
            cleared_squads=original,
            panel_ok=ok,
        )
    return True


async def finalize_expired(db: AsyncSession, subscription: Subscription) -> bool:
    """Завершить free-окно (ветка B): снять free-сквад и вернуть платный доступ.

    Вызывается когда ``expire_free_until <= now``. Два исхода:
    - подписка ещё ОПЛАЧЕНА (``end_date`` в будущем) — free-окно было лишь бесплатным
      «хвостом» перед возвращением на платные сервера: восстанавливаем реальные сквады,
      поднимаем статус ACTIVE, чистим маркеры. Доступ НЕ отрубаем — человек заплатил.
    - подписка действительно истекла (``end_date`` в прошлом/нет) — снимаем free-сквад,
      переводим в EXPIRED; ``expire_disabled_squads`` сохраняется для восстановления
      при будущем продлении.

    Идемпотентна: если free-окна нет — тихо False.
    """
    if not has_expire_disabled_squads(subscription):
        return False
    if getattr(subscription, 'expire_free_until', None) is None:
        return False

    from app.database.crud.user import get_user_by_id

    user = await get_user_by_id(db, subscription.user_id)
    panel_user_id = _resolve_panel_user_id(subscription, user) if user else None
    end_date = getattr(subscription, 'end_date', None)

    # [Форк-фикс] Восстанавливаем платный доступ только когда free-окно ДЕЙСТВИТЕЛЬНО
    # завершилось (expire_free_until в прошлом) — вызывающая выборка и сама функция
    # работают на этой посылке. Если окно ещё активно (expire_free_until в будущем), а
    # end_date уже жив (внешнее продление мимо бота в активном окне) — это «спящее»
    # воскрешение: ничего не делаем, откладываем до реального завершения окна. Без гарда
    # сработал бы now-переход (restore → ACTIVE, очистка сквадов), панель увидела бы
    # конфликтующий expireAt (будущий срок окна) и выбила бы юзера в EXPIRED — потеря
    # доступа при живом end_date.
    if user and is_free_window_active(subscription, now=datetime.now(UTC)):
        return False

    still_paid = end_date is not None and _aware(end_date) > datetime.now(UTC)

    if still_paid:
        # Подписка оплачена вперёд: возвращаем реальные сквады и поднимаем активный статус.
        # Держим маркеры — их чистит восстановление (как в _push_restore_to_panel штатного
        # продления). Здесь тоже восстанавливаем через штатный update (status/expire по end_date).
        apply_restore_fields(subscription)
        subscription.status = SubscriptionStatus.ACTIVE.value

        # [Форк-фикс] free-сквад тарифа при восстановлении надо УБРАТЬ из connected, иначе он
        # залипнет рядом с реальными (панель будет видеть и платный, и free-сквады). Это же
        # вычитание делает apply_restore_fields при штатном продлении; здесь тариф ещё задан,
        # поэтому убираем временно (apply_restore_fields уже не вычтет — маркеры чисты).
        tariff_free = set(_tariff_free_squads(subscription))
        connected_restored = [uuid for uuid in (subscription.connected_squads or []) if uuid not in tariff_free]
        subscription.connected_squads = connected_restored

        await db.commit()
        await db.refresh(subscription)

        ok = await _push_restore_to_panel(db, subscription)
        logger.info(
            'expire-squad: free-окно завершено, подписка ещё оплачена — доступ восстановлен',
            subscription_id=subscription.id,
            user_id=subscription.user_id,
            end_date=_aware(end_date).isoformat(),
            restored_squads=subscription.connected_squads,
            panel_ok=ok,
        )
        return True

    subscription.connected_squads = []
    subscription.expire_free_until = None
    subscription.status = SubscriptionStatus.EXPIRED.value
    await db.commit()
    await db.refresh(subscription)

    ok = False
    if panel_user_id:
        ok = await _push_to_panel(
            subscription,
            panel_user_id,
            active_squads=[],
            expire_at=_aware(subscription.end_date),
            status=SubscriptionStatus.EXPIRED,
        )
    logger.info(
        'expire-squad: free-окно завершено, сквады сняты (ветка B → EXPIRED)',
        subscription_id=subscription.id,
        user_id=subscription.user_id,
        panel_ok=ok,
    )
    return True


async def handle_expiration_by_id(db: AsyncSession, subscription_id: int) -> bool:
    """Догрузить подписку (+тариф, +юзер) по id и обработать истечение.

    Точка входа для sync-путей: батч собирает id кандидатов и обрабатывает их ПОСЛЕ
    финального commit (панельный push нельзя делать в середине батча).
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.database.crud.user import get_user_by_id

    result = await db.execute(
        select(Subscription).where(Subscription.id == subscription_id).options(selectinload(Subscription.tariff))
    )
    subscription = result.scalar_one_or_none()
    if subscription is None:
        return False

    user = await get_user_by_id(db, subscription.user_id)
    return await handle_expiration(db, user, subscription)


def apply_restore_fields(subscription: Subscription) -> bool:
    """Чистая (без I/O) мутация полей подписки при восстановлении реальных сквадов.

    Возвращает отложенные при истечении сквады в ``connected_squads`` (сохраняя порядок, без
    дублей), очищает маркеры ``expire_disabled_squads`` и ``expire_free_until``. Коммит и
    синхронизацию с панелью делает вызывающий (reset-флоу продления/смены тарифа сам пушит
    connected_squads). Возвращает True, если было что восстанавливать.

    Ветка B: во free-окне ``connected_squads`` содержит выданный free-сквад, а реальные лежат
    в ``expire_disabled_squads``. При восстановлении free-сквад надо УБРАТЬ (подписка снова
    платная), иначе он залипнет рядом с реальными. Поэтому перед merge вычитаем free-сквады
    тарифа из текущего ``connected``. Ветка A: ``connected`` пуст → вычитать нечего.
    """
    if not has_expire_disabled_squads(subscription):
        return False

    disabled = list(subscription.expire_disabled_squads or [])
    free_squads = set(_tariff_free_squads(subscription))
    connected = [uuid for uuid in (subscription.connected_squads or []) if uuid not in free_squads]
    merged = connected + [uuid for uuid in disabled if uuid not in connected]

    subscription.connected_squads = merged
    subscription.expire_disabled_squads = []
    subscription.expire_free_until = None
    return True


async def restore_squads(db: AsyncSession, subscription: Subscription, *, reason: str) -> bool:
    """Вернуть ранее отложенные реальные сквады и синхронизировать панель.

    Для точек без собственного пуша (реактивация мимо reset-флоу). Возвращает сквады из
    ``expire_disabled_squads`` в ``connected_squads``, очищает маркеры, затем пушит полный
    список сквадов на панель. Идемпотентна: если восстанавливать нечего — тихо False.
    """
    disabled = list(subscription.expire_disabled_squads or [])
    if not apply_restore_fields(subscription):
        return False

    await db.commit()
    await db.refresh(subscription)

    from app.database.crud.user import get_user_by_id

    user = await get_user_by_id(db, subscription.user_id)
    panel_user_id = _resolve_panel_user_id(subscription, user) if user else None
    ok = False
    if panel_user_id:
        # Восстановление идёт при продлении → подписка снова активна: пушим сквады через
        # штатный update_remnawave_user (он выставит корректный статус/expire по end_date).
        ok = await _push_restore_to_panel(db, subscription)

    logger.info(
        'expire-squad: реальные сквады восстановлены',
        subscription_id=subscription.id,
        user_id=subscription.user_id,
        restored_squads=disabled,
        reason=reason,
        panel_ok=ok,
    )
    return True


async def _push_to_panel(
    subscription: Subscription,
    panel_user_id: int,
    *,
    active_squads: list[str],
    expire_at: datetime | None,
    status: SubscriptionStatus,
) -> bool:
    """Прямой PATCH на панель: сквады + expireAt + статус (для веток A/B).

    НЕ переиспользует ``update_remnawave_user``: тот пересчитывает статус/expire из
    is_actually_active по ``end_date`` (в прошлом при истечении) и отправил бы DISABLED +
    прошлый expire, убив free-окно. Здесь пушим ровно то, что решила фича. Пустой список
    сквадов ``[]`` доходит корректно (гард update_user = ``is not None``).

    ВАЖНО: панель RemnaWave валидирует PATCH /api/users и отклоняет ВЕСЬ запрос
    (``Validation failed``, сквады не снимутся) при ЛЮБОМ из нарушений:
    - ``status`` = LIMITED/EXPIRED — эти статусы ВЫЧИСЛЯЕМЫЕ, задать нельзя (валидны только
      ACTIVE/DISABLED). Для ветки A статус НЕ шлём вовсе — панель сама выставит EXPIRED по
      прошедшему expireAt. Для ветки B шлём ACTIVE явно (там expireAt в будущем).
    - ``expireAt`` в прошлом — «Expiration date cannot be in the past». Для ветки A expireAt
      трогать НЕ нужно (он уже прошлый на панели, потому и истекло) — НЕ шлём его. Для ветки B
      он в будущем — шлём. Правило: отправляем expire_at только если он строго в будущем.
    """
    try:
        from app.external.remnawave_api import UserStatus
        from app.services.subscription_service import SubscriptionService

        # ACTIVE шлём явно; EXPIRED — не шлём (панель вычислит из прошлого expireAt).
        panel_status = UserStatus.ACTIVE if status == SubscriptionStatus.ACTIVE else None
        # expireAt шлём ТОЛЬКО если он в будущем (ветка B). Прошлый expireAt панель отвергает,
        # а для ветки A менять срок и не нужно — снимаем лишь сквады.
        expire_at_arg = expire_at if (expire_at is not None and expire_at > datetime.now(UTC)) else None
        service = SubscriptionService()
        async with service.get_api_client() as api:
            await api.update_user(
                user_id=panel_user_id,
                status=panel_status,
                expire_at=expire_at_arg,
                active_internal_squads=list(active_squads),
            )
        return True
    except Exception as error:
        logger.error(
            'expire-squad: ошибка синхронизации с панелью',
            subscription_id=getattr(subscription, 'id', None),
            error=str(error),
        )
        return False


async def _push_restore_to_panel(db: AsyncSession, subscription: Subscription) -> bool:
    """Пуш восстановленных сквадов через штатный update_remnawave_user (grace/multi-safe).

    При восстановлении подписка снова активна (end_date продлён вызывающим), поэтому штатный
    путь выставит корректный статус/expire; sync_squads=True прокинет полный connected_squads.
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
        return updated is not None
    except Exception as error:
        logger.error(
            'expire-squad: ошибка восстановления сквадов на панели',
            subscription_id=getattr(subscription, 'id', None),
            error=str(error),
        )
        return False
