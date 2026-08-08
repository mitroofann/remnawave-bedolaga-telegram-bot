"""add landing_pages.trial_enabled + guest_purchases.is_trial (landing trial feature)

Revision ID: 0104_landing_trial
Revises: 0103
Create Date: 2026-07-30

NB: перепривязано с 0100 на 0103 при мерже upstream/main — апстрим занял 0101
(lava subscriptions), 0102, 0103, поэтому landing-trial уходит в хвост цепочки.

NB2 (мерж upstream v4.0.0 / Remnawave 3.0.0): апстрим добавил СВОЮ ревизию
'0104' (0104_remnawave_numeric_id, down='0103') — коллизия с нашим прежним
revision='0104'/down='0103'. Разведено переименованием НАШЕЙ ревизии в
'0104_landing_trial' (файл не переименован намеренно). down остаётся '0103';
numeric_id перецеплена в ХВОСТ цепочки (down='0108'), потому что прод форка уже
застемплен на 0108 — вставь её «в прошлое», и `upgrade head` пропустил бы
создание числовых колонок (см. [[alembic-merge-revision-collision]]).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0104_landing_trial'
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
