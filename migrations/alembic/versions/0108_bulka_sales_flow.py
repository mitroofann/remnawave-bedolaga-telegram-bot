"""Additive Bulka authenticated landing sales-flow fields.

Revision ID: 0108
Revises: 0107
"""

import sqlalchemy as sa
from alembic import op


revision = '0108'
down_revision = '0107'
branch_labels = None
depends_on = None


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                'SELECT EXISTS (SELECT 1 FROM information_schema.columns '
                'WHERE table_name = :tbl AND column_name = :col)'
            ),
            {'tbl': table, 'col': column},
        ).scalar()
    )


def _has_constraint(conn, table: str, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                'SELECT EXISTS (SELECT 1 FROM information_schema.table_constraints '
                'WHERE table_name = :tbl AND constraint_name = :name)'
            ),
            {'tbl': table, 'name': name},
        ).scalar()
    )


def _has_index(conn, name: str) -> bool:
    return bool(conn.execute(sa.text('SELECT to_regclass(:name)'), {'name': name}).scalar())


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_column(conn, 'landing_pages', 'template'):
        op.add_column(
            'landing_pages',
            sa.Column('template', sa.String(length=32), nullable=False, server_default='classic'),
        )
    if not _has_constraint(conn, 'landing_pages', 'chk_landing_template'):
        op.create_check_constraint(
            'chk_landing_template',
            'landing_pages',
            "template IN ('classic', 'bulka_sales_flow')",
        )

    columns = (
        ('landing_slug', sa.String(length=100)),
        ('landing_template', sa.String(length=32)),
        ('flow_kind', sa.String(length=20)),
        ('selected_tariff_id', sa.Integer()),
        ('selected_period_days', sa.Integer()),
        ('idempotency_key', sa.String(length=36)),
        ('idempotency_payload_hash', sa.String(length=64)),
        ('payment_url', sa.Text()),
        ('flow_return_kind', sa.String(length=50)),
        ('activated_at', sa.DateTime(timezone=True)),
        ('subscription_id', sa.Integer()),
    )
    for name, column_type in columns:
        if not _has_column(conn, 'guest_purchases', name):
            op.add_column('guest_purchases', sa.Column(name, column_type, nullable=True))

    if not _has_constraint(conn, 'guest_purchases', 'chk_guest_purchase_landing_template'):
        op.create_check_constraint(
            'chk_guest_purchase_landing_template',
            'guest_purchases',
            "landing_template IS NULL OR landing_template IN ('bulka_sales_flow')",
        )
    if not _has_constraint(conn, 'guest_purchases', 'chk_guest_purchase_flow_kind'):
        op.create_check_constraint(
            'chk_guest_purchase_flow_kind',
            'guest_purchases',
            "flow_kind IS NULL OR flow_kind IN ('trial', 'purchase')",
        )
    if not _has_index(conn, 'uq_guest_purchases_bulka_idempotency'):
        op.create_index(
            'uq_guest_purchases_bulka_idempotency',
            'guest_purchases',
            ['user_id', 'landing_id', 'idempotency_key'],
            unique=True,
            postgresql_where=sa.text('idempotency_key IS NOT NULL'),
        )
    if not _has_constraint(conn, 'guest_purchases', 'fk_guest_purchases_subscription_id'):
        op.create_foreign_key(
            'fk_guest_purchases_subscription_id',
            'guest_purchases',
            'subscriptions',
            ['subscription_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _has_constraint(conn, 'guest_purchases', 'fk_guest_purchases_subscription_id'):
        op.drop_constraint('fk_guest_purchases_subscription_id', 'guest_purchases', type_='foreignkey')
    if _has_index(conn, 'uq_guest_purchases_bulka_idempotency'):
        op.drop_index('uq_guest_purchases_bulka_idempotency', table_name='guest_purchases')
    if _has_constraint(conn, 'guest_purchases', 'chk_guest_purchase_flow_kind'):
        op.drop_constraint('chk_guest_purchase_flow_kind', 'guest_purchases', type_='check')
    if _has_constraint(conn, 'guest_purchases', 'chk_guest_purchase_landing_template'):
        op.drop_constraint('chk_guest_purchase_landing_template', 'guest_purchases', type_='check')
    for name in (
        'subscription_id',
        'activated_at',
        'flow_return_kind',
        'payment_url',
        'idempotency_payload_hash',
        'idempotency_key',
        'selected_period_days',
        'selected_tariff_id',
        'flow_kind',
        'landing_template',
        'landing_slug',
    ):
        if _has_column(conn, 'guest_purchases', name):
            op.drop_column('guest_purchases', name)

    if _has_constraint(conn, 'landing_pages', 'chk_landing_template'):
        op.drop_constraint('chk_landing_template', 'landing_pages', type_='check')
    if _has_column(conn, 'landing_pages', 'template'):
        op.drop_column('landing_pages', 'template')
