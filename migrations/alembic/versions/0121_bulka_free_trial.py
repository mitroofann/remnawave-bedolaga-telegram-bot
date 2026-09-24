"""Durable authenticated Bulka free-trial claims.

Revision ID: 0121
Revises: 0120_partner_referral_legacy
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0121'
down_revision: Union[str, None] = '0120_partner_referral_legacy'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return True
    return column in {item['name'] for item in inspector.get_columns(table)}


def _has_index(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(item['name'] == name for item in inspector.get_indexes(table))


def _has_constraint(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(item.get('name') == name for item in inspector.get_foreign_keys(table))


def upgrade() -> None:
    if not _has_column('guest_purchases', 'is_bulka_free_trial'):
        op.add_column(
            'guest_purchases',
            sa.Column('is_bulka_free_trial', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        )
    if not _has_column('guest_purchases', 'activation_kind'):
        op.add_column('guest_purchases', sa.Column('activation_kind', sa.String(length=32), nullable=True))
    if not _has_column('guest_purchases', 'activation_lease_id'):
        op.add_column('guest_purchases', sa.Column('activation_lease_id', sa.String(length=36), nullable=True))
    if not _has_column('guest_purchases', 'activation_lease_until'):
        op.add_column(
            'guest_purchases',
            sa.Column('activation_lease_until', sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column('guest_purchases', 'activation_attempts'):
        op.add_column(
            'guest_purchases',
            sa.Column('activation_attempts', sa.Integer(), nullable=False, server_default='0'),
        )
    if not _has_column('subscriptions', 'source_guest_purchase_id'):
        op.add_column('subscriptions', sa.Column('source_guest_purchase_id', sa.Integer(), nullable=True))
    if not _has_constraint('subscriptions', 'fk_subscriptions_source_guest_purchase'):
        op.create_foreign_key(
            'fk_subscriptions_source_guest_purchase',
            'subscriptions',
            'guest_purchases',
            ['source_guest_purchase_id'],
            ['id'],
            ondelete='SET NULL',
        )
    if not _has_index('subscriptions', 'uq_subscriptions_source_guest_purchase'):
        op.create_index('uq_subscriptions_source_guest_purchase', 'subscriptions', ['source_guest_purchase_id'], unique=True)
    if not _has_index('guest_purchases', 'uq_guest_purchases_bulka_free_user'):
        op.create_index(
            'uq_guest_purchases_bulka_free_user',
            'guest_purchases',
            ['buyer_user_id'],
            unique=True,
            postgresql_where=sa.text('is_bulka_free_trial IS TRUE'),
        )


def downgrade() -> None:
    if _has_index('guest_purchases', 'uq_guest_purchases_bulka_free_user'):
        op.drop_index('uq_guest_purchases_bulka_free_user', table_name='guest_purchases')
    if _has_index('subscriptions', 'uq_subscriptions_source_guest_purchase'):
        op.drop_index('uq_subscriptions_source_guest_purchase', table_name='subscriptions')
    if _has_constraint('subscriptions', 'fk_subscriptions_source_guest_purchase'):
        op.drop_constraint('fk_subscriptions_source_guest_purchase', 'subscriptions', type_='foreignkey')
    if _has_column('subscriptions', 'source_guest_purchase_id'):
        op.drop_column('subscriptions', 'source_guest_purchase_id')
    if _has_column('guest_purchases', 'activation_attempts'):
        op.drop_column('guest_purchases', 'activation_attempts')
    if _has_column('guest_purchases', 'activation_lease_until'):
        op.drop_column('guest_purchases', 'activation_lease_until')
    if _has_column('guest_purchases', 'activation_lease_id'):
        op.drop_column('guest_purchases', 'activation_lease_id')
    if _has_column('guest_purchases', 'activation_kind'):
        op.drop_column('guest_purchases', 'activation_kind')
    if _has_column('guest_purchases', 'is_bulka_free_trial'):
        op.drop_column('guest_purchases', 'is_bulka_free_trial')
