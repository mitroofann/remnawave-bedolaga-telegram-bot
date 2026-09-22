"""Add per-partner legacy referral reward overrides.

Revision ID: 0120_partner_referral_legacy
Revises: 0119

[Fork] Partner-scoped legacy reward policy is kept outside system_settings,
which remains process-global and may be pinned by the environment.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0120_partner_referral_legacy'
down_revision: Union[str, None] = '0119'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = 'partner_referral_legacy_overrides'


def _has_table(conn: sa.Connection) -> bool:
    return bool(
        conn.execute(
            sa.text(
                'SELECT EXISTS (SELECT 1 FROM information_schema.tables '
                'WHERE table_schema = current_schema() AND table_name = :table)'
            ),
            {'table': _TABLE},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn):
        return

    op.create_table(
        _TABLE,
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('partner_user_id', sa.Integer(), nullable=False),
        sa.Column('minimum_topup_kopeks', sa.Integer(), nullable=True),
        sa.Column('first_topup_bonus_kopeks', sa.Integer(), nullable=True),
        sa.Column('inviter_bonus_kopeks', sa.Integer(), nullable=True),
        sa.Column('commission_percent', sa.Integer(), nullable=True),
        sa.Column('first_payment_commission_percent', sa.Integer(), nullable=True),
        sa.Column('recurring_commission_tiers', sa.Text(), nullable=True),
        sa.Column('max_commission_payments', sa.Integer(), nullable=True),
        sa.Column('max_commission_kopeks', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['partner_user_id'], ['users.id'], name='fk_partner_referral_legacy_override_user', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('partner_user_id', name='uq_partner_referral_legacy_override_user'),
    )
    op.create_index(
        'ix_partner_referral_legacy_overrides_id',
        _TABLE,
        ['id'],
        unique=False,
    )
    op.create_index(
        'ix_partner_referral_legacy_overrides_partner_user_id',
        _TABLE,
        ['partner_user_id'],
        unique=False,
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn):
        return
    op.drop_index('ix_partner_referral_legacy_overrides_partner_user_id', table_name=_TABLE)
    op.drop_index('ix_partner_referral_legacy_overrides_id', table_name=_TABLE)
    op.drop_table(_TABLE)
