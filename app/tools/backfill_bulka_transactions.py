"""Backfill missing balance Transactions for already-paid Bulka landing purchases.

Isolated fork feature (Bulka sales flow). Historical paid Bulka purchases were
delivered without the accounting Transaction that the classic landing path
creates, so they never appeared in the cabinet payment journal
(GET /cabinet/balance/transactions reads Transaction rows by user_id). This
one-shot, idempotent tool creates the missing rows.

It reuses the SAME helper the runtime handler uses
(``landing_bulka_flow_service.record_bulka_transaction``), so both share the
exact "transaction already exists" predicate — external_id == payment_id scoped
by payment method + SUBSCRIPTION_PAYMENT — and never produce duplicates, whether
run before or after the handler fix ships.

Examples:
    python -m app.tools.backfill_bulka_transactions            # dry-run (default)
    python -m app.tools.backfill_bulka_transactions --apply    # actually write
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from sqlalchemy import select

from app.database.database import AsyncSessionLocal
from app.database.models import GuestPurchase, GuestPurchaseStatus
from app.services.guest_purchase_service import _resolve_payment_method
from app.services.landing_bulka_flow_service import (
    _TEMPLATE,
    _bulka_transaction_exists,
    record_bulka_transaction,
)


def _candidates_query():
    """Delivered, paid, account-linked, non-gift, non-free Bulka purchases."""
    return (
        select(GuestPurchase)
        .where(
            GuestPurchase.landing_template == _TEMPLATE,
            GuestPurchase.status == GuestPurchaseStatus.DELIVERED.value,
            GuestPurchase.user_id.isnot(None),
            GuestPurchase.payment_id.isnot(None),
            GuestPurchase.is_gift.is_(False),
            GuestPurchase.amount_kopeks > 0,
        )
        .order_by(GuestPurchase.id)
    )


async def _run(*, apply: bool) -> int:
    inspected = 0
    already = 0
    created = 0
    skipped = 0

    async with AsyncSessionLocal() as db:
        result = await db.execute(_candidates_query())
        purchases = result.scalars().all()

        for purchase in purchases:
            inspected += 1
            payment_method_enum = _resolve_payment_method(purchase.payment_method)
            payment_method_value = payment_method_enum.value if payment_method_enum else None

            exists = await _bulka_transaction_exists(
                db,
                user_id=purchase.user_id,
                external_id=purchase.payment_id,
                payment_method_value=payment_method_value,
            )
            if exists:
                already += 1
                continue

            if not apply:
                # Dry-run: count what would be created without writing.
                created += 1
                continue

            # Reuse the runtime helper. fire_side_effects=False: this is a
            # historical backfill, we must not fire stale Yandex conversions /
            # contest events. created_at=paid_at so the journal shows the real
            # payment date, not "now".
            transaction = await record_bulka_transaction(
                db,
                purchase,
                fire_side_effects=False,
                created_at=purchase.paid_at,
            )
            if transaction is not None:
                created += 1
            else:
                # Lost a race, or helper's own guards rejected it. Not an error.
                skipped += 1

    summary: dict[str, Any] = {
        'apply': apply,
        'inspected': inspected,
        'already_had_transaction': already,
        'created' if apply else 'would_create': created,
        'skipped': skipped,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not apply:
        print('Dry-run only. Add --apply to write the missing transactions.')
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Backfill missing Bulka purchase transactions (dry-run by default)')
    parser.add_argument('--apply', action='store_true', help='Actually create the missing transactions')
    return parser


def main() -> None:
    raise SystemExit(asyncio.run(_run(apply=_parser().parse_args().apply)))


if __name__ == '__main__':
    main()
