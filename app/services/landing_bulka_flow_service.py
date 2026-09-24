"""Authenticated Bulka landing sales flow, isolated from classic guest checkout."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.landing import (
    create_guest_purchase,
    get_bulka_purchase_by_idempotency_key,
)
from app.database.crud.subscription import (
    create_paid_subscription,
    create_trial_subscription,
    get_subscription_by_user_id,
    replace_subscription,
)
from app.database.crud.tariff import get_tariff_by_id
from app.database.crud.transaction import create_transaction, emit_transaction_side_effects
from app.database.models import (
    GuestPurchase,
    GuestPurchaseStatus,
    LandingPage,
    Subscription,
    Transaction,
    TransactionType,
    User,
)
from app.services.guest_purchase_service import (
    GuestPurchaseError,
    _resolve_payment_method,
    validate_and_calculate,
)
from app.services.landing_trial_service import is_landing_trial_globally_enabled, resolve_landing_trial_params
from app.services.payment_method_config_service import _get_method_defaults
from app.services.payment_service import PaymentService
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)

_TEMPLATE = 'bulka_sales_flow'
_USERNAME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]{4,31}$')


@dataclass(slots=True)
class BulkaPurchaseResult:
    purchase: GuestPurchase
    payment_url: str


@dataclass(slots=True)
class BulkaFreeTrialResult:
    purchase: GuestPurchase


_FREE_TRIAL_ACTIVATION_KIND = 'free_trial'
_FREE_TRIAL_LEASE_SECONDS = 30


def _error(code: str, message: str, status_code: int = 400) -> GuestPurchaseError:
    error = GuestPurchaseError(message, status_code)
    error.code = code  # type: ignore[attr-defined]
    return error


def _assert_bulka_landing(landing: LandingPage | None) -> LandingPage:
    if landing is None:
        raise _error('landing_not_found', 'Landing page not found', 404)
    if not getattr(landing, 'is_active', True):
        raise _error('landing_not_found', 'Landing page not found', 404)
    if landing.template != _TEMPLATE:
        raise _error('unsupported_landing_template', 'This landing does not support Bulka flow', 409)
    return landing


def _contact_for_user(user: User) -> tuple[str, str]:
    if user.email and user.email_verified:
        return 'email', user.email
    username = (user.username or '').lstrip('@')
    if _USERNAME_RE.match(username):
        return 'telegram', f'@{username}'
    raise _error('fulfillment_contact_required', 'A verified email or Telegram username is required', 409)


def _payload_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _method_config(landing: LandingPage, method_id: str, sub_option: str | None) -> tuple[dict, str]:
    method = next((m for m in (landing.payment_methods or []) if m.get('method_id') == method_id), None)
    if method is None:
        raise _error('payment_method_not_allowed', 'Payment method is not available on this landing')
    if sub_option:
        defaults = _get_method_defaults().get(method_id, {})
        known = {item['id'] for item in (defaults.get('available_sub_options') or [])}
        enabled = method.get('sub_options')
        if sub_option not in known or (enabled is not None and not enabled.get(sub_option, True)):
            raise _error('payment_sub_option_not_allowed', 'Payment sub-option is not available on this landing')
        return method, f'{method_id}_{sub_option}'
    return method, method_id


async def _assert_trial_eligible(db: AsyncSession, landing: LandingPage, user: User) -> None:
    if not is_landing_trial_globally_enabled() or not landing.trial_enabled:
        raise _error('trial_unavailable', 'Trial is not enabled for this landing', 409)
    auth_type = 'email' if user.email and user.email_verified else 'telegram'
    if settings.is_trial_disabled_for_user(auth_type):
        raise _error('trial_account_type_restricted', 'Trial is not available for this account type', 409)
    await db.refresh(user, ['subscriptions'])
    if user.is_trial_already_used():
        raise _error('trial_already_used', 'Trial already used', 409)


async def _lock_user(db: AsyncSession, user_id: int) -> User:
    result = await db.execute(select(User).where(User.id == user_id).with_for_update())
    locked = result.scalars().first()
    if locked is None:
        raise _error('user_not_found', 'User not found', 404)
    await db.refresh(locked, ['subscriptions'])
    return locked


async def _assert_free_trial_configuration(db: AsyncSession, landing: LandingPage) -> None:
    if not is_landing_trial_globally_enabled() or not landing.trial_enabled:
        raise _error('trial_unavailable', 'Trial is not enabled for this landing', 409)
    params = await resolve_landing_trial_params(db)
    if params.requires_payment or params.price_kopeks != 0:
        raise _error('trial_requires_payment', 'This trial requires payment', 409)
    if params.tariff_id is None or params.duration_days <= 0:
        raise _error('trial_configuration_invalid', 'Free trial is not configured', 500)
    if await get_tariff_by_id(db, params.tariff_id) is None:
        raise _error('trial_tariff_missing', 'Trial tariff is not configured', 500)


async def _get_free_trial_claim(
    db: AsyncSession, *, user_id: int, landing_id: int, idempotency_key: str, lock: bool = False
) -> GuestPurchase | None:
    statement = select(GuestPurchase).where(
        GuestPurchase.buyer_user_id == user_id,
        GuestPurchase.landing_id == landing_id,
        GuestPurchase.idempotency_key == idempotency_key,
        GuestPurchase.is_bulka_free_trial.is_(True),
    )
    if lock:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    return result.scalars().first()


async def _claim_free_trial_lease(db: AsyncSession, purchase_id: int) -> str | None:
    now = datetime.now(UTC)
    lease_id = str(uuid4())
    result = await db.execute(
        update(GuestPurchase)
        .where(
            GuestPurchase.id == purchase_id,
            GuestPurchase.is_bulka_free_trial.is_(True),
            GuestPurchase.status == GuestPurchaseStatus.PENDING_ACTIVATION.value,
            (GuestPurchase.activation_lease_until.is_(None) | (GuestPurchase.activation_lease_until <= now)),
        )
        .values(
            activation_lease_id=lease_id,
            activation_lease_until=now + timedelta(seconds=_FREE_TRIAL_LEASE_SECONDS),
            activation_attempts=GuestPurchase.activation_attempts + 1,
        )
    )
    await db.commit()
    return lease_id if result.rowcount == 1 else None


async def build_bulka_flow_config(db: AsyncSession, landing: LandingPage, user: User) -> dict:
    _assert_bulka_landing(landing)
    trial_available = True
    unavailable_code = unavailable_reason = None
    try:
        await _assert_trial_eligible(db, landing, user)
    except GuestPurchaseError as exc:
        trial_available = False
        unavailable_code = getattr(exc, 'code', 'trial_unavailable')
        unavailable_reason = exc.message
    params = await resolve_landing_trial_params(db)
    trial_tariff = await get_tariff_by_id(db, params.tariff_id) if params.tariff_id else None
    tariffs = []
    for tariff_id in landing.allowed_tariff_ids or []:
        tariff = await get_tariff_by_id(db, tariff_id)
        if tariff is None or not tariff.is_active:
            continue
        periods = []
        allowed = (landing.allowed_periods or {}).get(str(tariff.id), tariff.get_purchasable_periods())
        for days in sorted(allowed):
            try:
                _tariff, price = await validate_and_calculate(db, landing, tariff.id, days)
            except GuestPurchaseError:
                continue
            base = tariff.get_purchasable_price_for_period(days)
            periods.append(
                {
                    'days': days,
                    'price_kopeks': price,
                    'original_price_kopeks': base if base != price else None,
                    'discount_percent': (round((1 - price / base) * 100) if base and base != price else None),
                }
            )
        if periods:
            tariffs.append(
                {
                    'id': tariff.id,
                    'name': tariff.name,
                    'description_html': tariff.description,
                    'traffic_limit_gb': tariff.traffic_limit_gb,
                    'device_limit': tariff.device_limit,
                    'is_daily': bool(tariff.is_daily),
                    'periods': periods,
                }
            )
    methods = []
    defaults = _get_method_defaults()
    for method in landing.payment_methods or []:
        method_id = method.get('method_id', '')
        known = defaults.get(method_id, {}).get('available_sub_options') or []
        enabled = method.get('sub_options')
        sub_options = [
            {'id': item['id'], 'name': item['name']}
            for item in known
            if enabled is None or enabled.get(item['id'], True)
        ]
        methods.append(
            {
                key: method.get(key)
                for key in (
                    'method_id',
                    'display_name',
                    'description',
                    'icon_url',
                    'sort_order',
                    'min_amount_kopeks',
                    'max_amount_kopeks',
                    'currency',
                )
            }
            | {'sub_options': sub_options or None}
        )
    methods.sort(key=lambda item: item['sort_order'] or 0)
    return {
        'landing_slug': landing.slug,
        'landing_template': _TEMPLATE,
        'trial': {
            'available': trial_available,
            'unavailable_code': unavailable_code,
            'unavailable_reason': unavailable_reason,
            'tariff_id': params.tariff_id,
            'tariff_name': trial_tariff.name if trial_tariff else None,
            'tariff_description_html': trial_tariff.description if trial_tariff else None,
            'duration_days': params.duration_days,
            'traffic_limit_gb': params.traffic_limit_gb,
            'device_limit': params.device_limit,
            'requires_external_payment': params.requires_payment,
            'price_kopeks': params.price_kopeks,
            'currency': 'RUB',
        },
        'tariffs': tariffs,
        'payment_methods': methods,
    }


async def create_bulka_free_trial(
    db: AsyncSession,
    *,
    landing: LandingPage,
    user: User,
    idempotency_key: str,
    language: str | None = None,
    referrer: str | None = None,
    subid: str | None = None,
) -> BulkaFreeTrialResult:
    """Reserve and provision one authenticated, zero-price Bulka trial.

    The purchase row is the durable claim. Admission is serialized by the user row
    and the partial unique claim index protects the invariant after this transaction
    releases its lock. Provisioning is resumed from the stored subscription id.
    """
    _assert_bulka_landing(landing)
    request_payload = {
        'landing_slug': landing.slug,
        'language': language,
        'referrer': referrer,
        'subid': subid,
    }
    fingerprint = _payload_hash(request_payload)

    existing = await _get_free_trial_claim(
        db, user_id=user.id, landing_id=landing.id, idempotency_key=idempotency_key, lock=True
    )
    if existing:
        if existing.idempotency_payload_hash != fingerprint:
            raise _error(
                'idempotency_payload_mismatch', 'Idempotency-Key was already used with a different request', 409
            )
        if existing.status == GuestPurchaseStatus.DELIVERED.value:
            return BulkaFreeTrialResult(existing)
        await _resume_free_trial(db, existing)
        await db.refresh(existing)
        return BulkaFreeTrialResult(existing)

    locked_user = await _lock_user(db, user.id)
    await db.refresh(landing)
    _assert_bulka_landing(landing)
    await _assert_free_trial_configuration(db, landing)
    await _assert_trial_eligible(db, landing, locked_user)
    if any(subscription.is_pending_trial for subscription in locked_user.subscriptions or []):
        raise _error('trial_claim_exists', 'Finish the existing trial checkout before claiming a free trial', 409)
    params = await resolve_landing_trial_params(db)
    tariff = await get_tariff_by_id(db, params.tariff_id)
    if tariff is None:
        raise _error('trial_tariff_missing', 'Trial tariff is not configured', 500)

    contact_type, contact_value = _contact_for_user(locked_user)
    try:
        purchase = await create_guest_purchase(
            db,
            commit=False,
            landing_id=landing.id,
            landing_slug=landing.slug,
            landing_template=_TEMPLATE,
            flow_kind='trial',
            selected_tariff_id=tariff.id,
            selected_period_days=params.duration_days,
            idempotency_key=idempotency_key,
            idempotency_payload_hash=fingerprint,
            flow_return_kind='bulka_connect',
            tariff_id=tariff.id,
            period_days=params.duration_days,
            amount_kopeks=0,
            contact_type=contact_type,
            contact_value=contact_value,
            source='landing',
            buyer_user_id=locked_user.id,
            user_id=locked_user.id,
            status=GuestPurchaseStatus.PENDING_ACTIVATION.value,
            is_trial=True,
            is_bulka_free_trial=True,
            activation_kind=_FREE_TRIAL_ACTIVATION_KIND,
            subid=subid,
            referrer=referrer,
        )
        await db.flush()
    except IntegrityError:
        await db.rollback()
        winner = await _get_free_trial_claim(
            db, user_id=user.id, landing_id=landing.id, idempotency_key=idempotency_key
        )
        if winner and winner.idempotency_payload_hash == fingerprint:
            await _resume_free_trial(db, winner)
            return BulkaFreeTrialResult(winner)
        raise _error('trial_claim_exists', 'A free trial has already been claimed', 409)

    subscription = await create_trial_subscription(
        db=db,
        user_id=locked_user.id,
        duration_days=params.duration_days,
        traffic_limit_gb=params.traffic_limit_gb,
        device_limit=params.device_limit,
        connected_squads=params.squads or None,
        tariff_id=tariff.id,
        commit=False,
        source_guest_purchase_id=purchase.id,
    )
    if (
        subscription.user_id != locked_user.id
        or not subscription.is_trial
        or subscription.source_guest_purchase_id != purchase.id
    ):
        await db.rollback()
        raise _error('trial_claim_exists', 'A free trial has already been claimed', 409)
    purchase.subscription_id = subscription.id
    await db.commit()
    await db.refresh(purchase)
    await _resume_free_trial(db, purchase)
    await db.refresh(purchase)
    return BulkaFreeTrialResult(purchase)


async def _resume_free_trial(db: AsyncSession, purchase: GuestPurchase) -> None:
    if purchase.status == GuestPurchaseStatus.DELIVERED.value:
        return
    lease_id = await _claim_free_trial_lease(db, purchase.id)
    if lease_id is None:
        return
    await db.refresh(purchase)
    if not purchase.subscription_id:
        return
    subscription = await db.get(Subscription, purchase.subscription_id)
    if (
        subscription is None
        or subscription.user_id != purchase.buyer_user_id
        or not subscription.is_trial
        or subscription.source_guest_purchase_id != purchase.id
    ):
        return
    service = SubscriptionService()
    if not service.is_configured:
        return
    try:
        panel_user = await service.create_remnawave_user(db, subscription)
    except Exception:
        logger.exception('Bulka free trial provisioning failed', purchase_id=purchase.id)
        return
    await db.refresh(subscription)
    if panel_user is None or not subscription.subscription_url:
        return
    result = await db.execute(
        update(GuestPurchase)
        .where(
            GuestPurchase.id == purchase.id,
            GuestPurchase.activation_lease_id == lease_id,
            GuestPurchase.status == GuestPurchaseStatus.PENDING_ACTIVATION.value,
        )
        .values(
            subscription_id=subscription.id,
            subscription_url=subscription.subscription_url,
            subscription_crypto_link=subscription.subscription_crypto_link,
            activated_at=datetime.now(UTC),
            delivered_at=datetime.now(UTC),
            status=GuestPurchaseStatus.DELIVERED.value,
            activation_lease_id=None,
            activation_lease_until=None,
        )
    )
    await db.commit()
    if result.rowcount != 1:
        logger.info('Bulka free trial finalization lost lease', purchase_id=purchase.id)


async def retry_stuck_bulka_free_trials(
    db: AsyncSession, *, stale_seconds: int = _FREE_TRIAL_LEASE_SECONDS, limit: int = 10
) -> int:
    result = await db.execute(
        select(GuestPurchase)
        .where(
            GuestPurchase.is_bulka_free_trial.is_(True),
            GuestPurchase.status == GuestPurchaseStatus.PENDING_ACTIVATION.value,
            (GuestPurchase.activation_lease_until.is_(None))
            | (GuestPurchase.activation_lease_until <= datetime.now(UTC)),
        )
        .order_by(GuestPurchase.created_at.asc())
        .limit(limit)
    )
    count = 0
    for purchase in result.scalars().all():
        await _resume_free_trial(db, purchase)
        count += 1
    return count


async def create_bulka_purchase(
    db: AsyncSession,
    *,
    landing: LandingPage,
    user: User,
    flow_kind: Literal['trial', 'purchase'],
    tariff_id: int | None,
    period_days: int | None,
    payment_method: str,
    payment_sub_option: str | None,
    idempotency_key: str,
    language: str | None,
    yandex_cid: str | None,
    referrer: str | None,
    subid: str | None,
) -> BulkaPurchaseResult:
    _assert_bulka_landing(landing)
    request_payload = {
        'flow_kind': flow_kind,
        'tariff_id': tariff_id,
        'period_days': period_days,
        'payment_method': payment_method,
        'payment_sub_option': payment_sub_option,
        'language': language,
        'yandex_cid': yandex_cid,
        'referrer': referrer,
        'subid': subid,
    }
    fingerprint = _payload_hash(request_payload)
    existing = await get_bulka_purchase_by_idempotency_key(
        db, user_id=user.id, landing_id=landing.id, idempotency_key=idempotency_key, lock=True
    )
    if existing:
        if existing.idempotency_payload_hash != fingerprint:
            raise _error(
                'idempotency_payload_mismatch', 'Idempotency-Key was already used with a different request', 409
            )
        if not existing.payment_url:
            raise _error('purchase_initializing', 'Purchase is still being initialized', 409)
        return BulkaPurchaseResult(existing, existing.payment_url)
    contact_type, contact_value = _contact_for_user(user)
    if flow_kind == 'trial':
        if tariff_id is not None or period_days is not None:
            raise _error('trial_selection_forbidden', 'Trial tariff and period are selected by the server')
        await _assert_trial_eligible(db, landing, user)
        params = await resolve_landing_trial_params(db)
        if params.tariff_id is None:
            raise _error('trial_tariff_missing', 'Trial tariff is not configured', 500)
        tariff = await get_tariff_by_id(db, params.tariff_id)
        if tariff is None:
            raise _error('trial_tariff_missing', 'Trial tariff not found', 500)
        selected_period, amount = params.duration_days, params.price_kopeks
    else:
        if tariff_id is None or period_days is None:
            raise _error('tariff_period_required', 'tariff_id and period_days are required')
        tariff, amount = await validate_and_calculate(db, landing, tariff_id, period_days)
        selected_period = period_days
    method_config, provider_method = _method_config(landing, payment_method, payment_sub_option)
    if method_config.get('min_amount_kopeks') is not None and amount < method_config['min_amount_kopeks']:
        raise _error('payment_amount_too_low', 'Amount is below the payment method minimum')
    if method_config.get('max_amount_kopeks') is not None and amount > method_config['max_amount_kopeks']:
        raise _error('payment_amount_too_high', 'Amount exceeds the payment method maximum')
    purchase = await create_guest_purchase(
        db,
        commit=False,
        landing_id=landing.id,
        landing_slug=landing.slug,
        landing_template=_TEMPLATE,
        flow_kind=flow_kind,
        selected_tariff_id=tariff.id,
        selected_period_days=selected_period,
        idempotency_key=idempotency_key,
        idempotency_payload_hash=fingerprint,
        flow_return_kind='bulka_connect',
        tariff_id=tariff.id,
        period_days=selected_period,
        amount_kopeks=amount,
        contact_type=contact_type,
        contact_value=contact_value,
        payment_method=provider_method,
        source='landing',
        buyer_user_id=user.id,
        user_id=user.id,
        status=GuestPurchaseStatus.PENDING.value,
        subid=subid,
        referrer=referrer,
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await get_bulka_purchase_by_idempotency_key(
            db, user_id=user.id, landing_id=landing.id, idempotency_key=idempotency_key
        )
        if existing and existing.idempotency_payload_hash == fingerprint and existing.payment_url:
            return BulkaPurchaseResult(existing, existing.payment_url)
        raise _error('purchase_initializing', 'Purchase is being initialized', 409)
    return_url = f'{(settings.CABINET_URL or "").rstrip("/")}/buy/success/{purchase.token}'
    result = await PaymentService().create_guest_payment(
        db=db,
        amount_kopeks=amount,
        payment_method=provider_method,
        description=f'{tariff.name} — {selected_period}d',
        purchase_token=purchase.token,
        return_url=return_url,
    )
    payment_url = result.get('payment_url') if result else None
    if not payment_url:
        await db.rollback()
        raise _error('payment_provider_unavailable', 'Payment provider is unavailable', 502)
    purchase.payment_url = payment_url
    await db.commit()
    return BulkaPurchaseResult(purchase, payment_url)


async def _bulka_transaction_exists(
    db: AsyncSession, *, user_id: int, external_id: str, payment_method_value: str | None
) -> bool:
    """Whether a balance Transaction already links to this Bulka purchase.

    The link is the same implicit pair the classic landing path uses:
    ``Transaction.external_id == GuestPurchase.payment_id`` scoped by payment
    method and the SUBSCRIPTION_PAYMENT type. Handler and backfill share this
    exact predicate so they never double-write the same purchase.
    """
    query = (
        select(Transaction.id)
        .where(
            Transaction.user_id == user_id,
            Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
            Transaction.external_id == external_id,
        )
        .limit(1)
    )
    if payment_method_value is not None:
        query = query.where(Transaction.payment_method == payment_method_value)
    result = await db.execute(query)
    return result.scalar_one_or_none() is not None


async def record_bulka_transaction(
    db: AsyncSession,
    purchase: GuestPurchase,
    *,
    tariff_name: str | None = None,
    fire_side_effects: bool = True,
    created_at: datetime | None = None,
) -> Transaction | None:
    """Create the balance Transaction for a delivered paid Bulka purchase (idempotent).

    Mirrors the classic landing path in ``guest_purchase_service.fulfill_purchase``:
    a SUBSCRIPTION_PAYMENT tied to the provider payment via ``external_id=payment_id``.
    Reuses the developer's ``create_transaction`` / ``_resolve_payment_method`` rather
    than reimplementing the logic. Returns the created row, or ``None`` when nothing
    should be recorded (gift, free trial, missing user/payment id, or already present).

    Args:
        fire_side_effects: run the deferred side-effects (Yandex conversion, promo
            group, contest) after commit. The handler wants them; the historical
            backfill passes ``False`` so it never fires stale conversions.
        created_at: override the transaction timestamp — the backfill passes the
            purchase's ``paid_at`` so the journal shows the real payment date.
    """
    if purchase.is_gift or (purchase.amount_kopeks or 0) <= 0:
        return None
    if purchase.user_id is None or not purchase.payment_id:
        return None

    payment_method_enum = _resolve_payment_method(purchase.payment_method)
    payment_method_value = payment_method_enum.value if payment_method_enum else None

    if await _bulka_transaction_exists(
        db,
        user_id=purchase.user_id,
        external_id=purchase.payment_id,
        payment_method_value=payment_method_value,
    ):
        return None

    if tariff_name is None:
        tariff = await get_tariff_by_id(db, purchase.selected_tariff_id or purchase.tariff_id)
        tariff_name = tariff.name if tariff else 'Bulka'
    period_days = purchase.selected_period_days or purchase.period_days

    # commit=False keeps the write inside the caller's transaction; the unique
    # constraint uq_transaction_external_id_method is the last line of defence
    # against a racing webhook that slipped past the existence check above.
    try:
        transaction = await create_transaction(
            db=db,
            user_id=purchase.user_id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=purchase.amount_kopeks,
            description=f'Покупка подписки через лендинг ({tariff_name}, {period_days} дн.)',
            payment_method=payment_method_enum,
            external_id=purchase.payment_id,
            is_completed=True,
            created_at=created_at,
            commit=False,
        )
        await db.commit()
    except IntegrityError:
        # Racing webhook already inserted the same (external_id, payment_method):
        # idempotent no-op, don't poison the session.
        await db.rollback()
        return None

    if fire_side_effects:
        try:
            await emit_transaction_side_effects(
                db,
                transaction,
                amount_kopeks=purchase.amount_kopeks,
                user_id=purchase.user_id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                payment_method=payment_method_enum,
                external_id=purchase.payment_id,
                is_completed=True,
                description=transaction.description or '',
            )
        except Exception:
            logger.warning('Bulka: failed to emit transaction side-effects', purchase_id=purchase.id)

    return transaction


async def fulfill_bulka_purchase(db: AsyncSession, purchase: GuestPurchase) -> GuestPurchase:
    """Fulfill a paid Bulka purchase; caller already owns the purchase row lock."""
    if purchase.activated_at or purchase.status != GuestPurchaseStatus.PAID.value:
        return purchase
    if purchase.user_id is None:
        purchase.status = GuestPurchaseStatus.PENDING_ACTIVATION.value
        await db.commit()
        return purchase
    user = await db.get(User, purchase.user_id)
    tariff = await get_tariff_by_id(db, purchase.selected_tariff_id or purchase.tariff_id)
    if user is None or tariff is None:
        purchase.status = GuestPurchaseStatus.PENDING_ACTIVATION.value
        await db.commit()
        return purchase
    try:
        if purchase.flow_kind == 'trial':
            await db.refresh(user, ['subscriptions'])
            if user.is_trial_already_used():
                purchase.status = GuestPurchaseStatus.PENDING_ACTIVATION.value
                await db.commit()
                return purchase
            params = await resolve_landing_trial_params(db)
            subscription = await create_trial_subscription(
                db=db,
                user_id=user.id,
                duration_days=purchase.selected_period_days or params.duration_days,
                traffic_limit_gb=params.traffic_limit_gb,
                device_limit=params.device_limit,
                connected_squads=params.squads or None,
                tariff_id=purchase.selected_tariff_id,
            )
        else:
            existing = await get_subscription_by_user_id(db, user.id)
            squads = list(tariff.allowed_squads or [])
            if existing is not None:
                existing.tariff_id = tariff.id
                subscription = await replace_subscription(
                    db,
                    existing,
                    duration_days=purchase.selected_period_days or purchase.period_days,
                    traffic_limit_gb=tariff.traffic_limit_gb,
                    device_limit=tariff.device_limit,
                    connected_squads=squads,
                    is_trial=False,
                    update_server_counters=True,
                )
            else:
                subscription = await create_paid_subscription(
                    db=db,
                    user_id=user.id,
                    duration_days=purchase.selected_period_days or purchase.period_days,
                    traffic_limit_gb=tariff.traffic_limit_gb,
                    device_limit=tariff.device_limit,
                    connected_squads=squads,
                    tariff_id=tariff.id,
                    update_server_counters=True,
                )
        await SubscriptionService().create_remnawave_user(db, subscription)
        await db.refresh(subscription)
        purchase.subscription_id = subscription.id
        purchase.subscription_url = subscription.subscription_url
        purchase.subscription_crypto_link = subscription.subscription_crypto_link
        purchase.user_id = user.id
        purchase.activated_at = datetime.now(UTC)
        purchase.delivered_at = purchase.activated_at
        purchase.status = GuestPurchaseStatus.DELIVERED.value
        await db.commit()
    except Exception:
        await db.rollback()
        purchase.status = GuestPurchaseStatus.PENDING_ACTIVATION.value
        await db.commit()
        return purchase

    # Delivery is committed above. Record the accounting Transaction so the paid
    # Bulka purchase shows up in the user's cabinet payment journal, exactly like
    # the classic landing path. Isolated from delivery: a failure here must not
    # roll back the already-delivered subscription, so it runs after the commit
    # with its own guard.
    try:
        await record_bulka_transaction(db, purchase, tariff_name=tariff.name)
    except Exception:
        logger.exception('Bulka: failed to record purchase transaction', purchase_id=purchase.id)
        try:
            await db.rollback()
        except Exception:
            pass
    return purchase
