"""[Форк] Per-partner overrides for the legacy referral reward policy.

This module is intentionally separate from the global Settings registry. A
partner override is only active while the owner has approved partner status;
ordinary referrers always use the global legacy settings.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PartnerReferralLegacyOverride, User
from app.utils.user_utils import get_effective_referral_commission_percent


@dataclass(frozen=True)
class LegacyReferralSettings:
    minimum_topup_kopeks: int
    first_topup_bonus_kopeks: int
    inviter_bonus_kopeks: int
    commission_percent: int
    first_payment_commission_percent: int | None
    recurring_commission_tiers: str
    max_commission_payments: int
    max_commission_kopeks: int


@dataclass(frozen=True)
class ResolvedLegacyReferralSettings:
    values: LegacyReferralSettings
    overrides: dict[str, Any]
    sources: dict[str, str]
    is_partner: bool


_OVERRIDE_FIELDS = (
    'minimum_topup_kopeks',
    'first_topup_bonus_kopeks',
    'inviter_bonus_kopeks',
    'commission_percent',
    'first_payment_commission_percent',
    'recurring_commission_tiers',
    'max_commission_payments',
    'max_commission_kopeks',
)


def _bounded_int(value: Any, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _global_values(referrer: User) -> dict[str, Any]:
    # [Форк] Only approved partners may use the historical per-user rate here.
    # Ordinary referrers retain the global legacy policy even if an old admin
    # field happens to contain a value for them.
    is_partner = bool(getattr(referrer, 'is_partner', False))
    commission_percent = (
        get_effective_referral_commission_percent(referrer)
        if is_partner
        else _bounded_int(settings.REFERRAL_COMMISSION_PERCENT, maximum=100)
    )
    return {
        'minimum_topup_kopeks': settings.REFERRAL_MINIMUM_TOPUP_KOPEKS,
        'first_topup_bonus_kopeks': settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS,
        'inviter_bonus_kopeks': settings.REFERRAL_INVITER_BONUS_KOPEKS,
        'commission_percent': commission_percent,
        'first_payment_commission_percent': settings.REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT,
        'recurring_commission_tiers': settings.REFERRAL_RECURRING_COMMISSION_TIERS or '',
        'max_commission_payments': settings.REFERRAL_MAX_COMMISSION_PAYMENTS,
        'max_commission_kopeks': settings.REFERRAL_MAX_COMMISSION_KOPEKS,
    }


def _normalize(values: dict[str, Any]) -> LegacyReferralSettings:
    first_percent = values['first_payment_commission_percent']
    if first_percent is not None:
        first_percent = _bounded_int(first_percent, maximum=100)
    return LegacyReferralSettings(
        minimum_topup_kopeks=_bounded_int(values['minimum_topup_kopeks']),
        first_topup_bonus_kopeks=_bounded_int(values['first_topup_bonus_kopeks']),
        inviter_bonus_kopeks=_bounded_int(values['inviter_bonus_kopeks']),
        commission_percent=_bounded_int(values['commission_percent'], maximum=100),
        first_payment_commission_percent=first_percent,
        recurring_commission_tiers=values['recurring_commission_tiers'] or '',
        max_commission_payments=_bounded_int(values['max_commission_payments']),
        max_commission_kopeks=_bounded_int(values['max_commission_kopeks']),
    )


async def resolve_legacy_referral_settings(
    db: AsyncSession,
    referrer: User,
) -> ResolvedLegacyReferralSettings:
    """Resolve one immutable policy snapshot for a legacy referral operation."""
    values = _global_values(referrer)
    overrides = dict.fromkeys(_OVERRIDE_FIELDS)
    sources = dict.fromkeys(_OVERRIDE_FIELDS, 'global')

    is_partner = bool(getattr(referrer, 'is_partner', False))
    if is_partner and getattr(referrer, 'referral_commission_percent', None) is not None:
        sources['commission_percent'] = 'partner_profile'
    if is_partner:
        result = await db.execute(
            select(PartnerReferralLegacyOverride).where(
                PartnerReferralLegacyOverride.partner_user_id == referrer.id,
            )
        )
        row = result.scalar_one_or_none()
        if row is not None:
            for field in _OVERRIDE_FIELDS:
                raw = getattr(row, field)
                overrides[field] = raw
                if raw is not None:
                    values[field] = raw
                    sources[field] = 'partner'

    return ResolvedLegacyReferralSettings(
        values=_normalize(values),
        overrides=overrides,
        sources=sources,
        is_partner=bool(getattr(referrer, 'is_partner', False)),
    )
