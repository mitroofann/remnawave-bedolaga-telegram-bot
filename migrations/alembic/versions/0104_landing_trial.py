"""add landing_pages.trial_enabled + guest_purchases.is_trial (landing trial feature)

Revision ID: 0104
Revises: 0103
Create Date: 2026-07-30

NB: перепривязано с 0100 на 0103 при мерже upstream/main — апстрим занял 0101
(lava subscriptions), 0102, 0103, поэтому landing-trial уходит в хвост цепочки.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0104'
down_revision: Union[str, None] = '0103'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table_name, column_name, column_def)
NEW_COLUMNS = (
    (
        'landing_pages',
        'trial_enabled',
        sa.Column('trial_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    ),
    (
        'guest_purchases',
        'is_trial',
        sa.Column('is_trial', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    ),
)


def _has_column(conn, table: str, column: str) -> bool:
    result = conn.execute(
        sa.text(
            'SELECT EXISTS (SELECT 1 FROM information_schema.columns '
            'WHERE table_name = :tbl AND column_name = :col)'
        ),
        {'tbl': table, 'col': column},
    )
    return bool(result.scalar())


def upgrade() -> None:
    conn = op.get_bind()
    for table, col_name, col_def in NEW_COLUMNS:
        if not _has_column(conn, table, col_name):
            op.add_column(table, col_def)


def downgrade() -> None:
    conn = op.get_bind()
    for table, col_name, _ in reversed(NEW_COLUMNS):
        if _has_column(conn, table, col_name):
            op.drop_column(table, col_name)
