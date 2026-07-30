"""[Форк] поля для фичи «гашение сквада при лимите трафика» (идемпотентно)

Добавляет:
- subscriptions.traffic_limit_disabled_squads (JSON, default []) — сквады, снятые у юзера
  из-за исчерпания трафика (перенесены из connected_squads);
- subscriptions.traffic_limit_panel_bytes (BigInteger, nullable) — поднятый панельный лимит
  (used+буфер) на время гашения, хранится точно чтобы монитор не выбил юзера повторно;
- tariffs.limit_disabled_squads (JSON, default []) — какие сквады гасить при лимите.

Изолированная фича форка (CUSTOM_TRAFFIC_LIMIT_SQUAD_ENABLED). Миграция идемпотентна
(information_schema) — безопасна на проде, на свежей БД (bootstrap из моделей + stamp head)
не прогоняется. См. [[deferred-squad-limit-feature]], [[alembic-merge-revision-collision]].

Revision ID: 0106
Revises: 0105
"""

import sqlalchemy as sa
from alembic import op


revision = '0106'
down_revision = '0105'
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


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_column(conn, 'subscriptions', 'traffic_limit_disabled_squads'):
        op.add_column(
            'subscriptions',
            sa.Column('traffic_limit_disabled_squads', sa.JSON(), nullable=True, server_default='[]'),
        )
    if not _has_column(conn, 'subscriptions', 'traffic_limit_panel_bytes'):
        op.add_column(
            'subscriptions',
            sa.Column('traffic_limit_panel_bytes', sa.BigInteger(), nullable=True),
        )
    if not _has_column(conn, 'tariffs', 'limit_disabled_squads'):
        op.add_column(
            'tariffs',
            sa.Column('limit_disabled_squads', sa.JSON(), nullable=True, server_default='[]'),
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _has_column(conn, 'tariffs', 'limit_disabled_squads'):
        op.drop_column('tariffs', 'limit_disabled_squads')
    if _has_column(conn, 'subscriptions', 'traffic_limit_original_gb'):
        op.drop_column('subscriptions', 'traffic_limit_original_gb')
    if _has_column(conn, 'subscriptions', 'traffic_limit_disabled_squads'):
        op.drop_column('subscriptions', 'traffic_limit_disabled_squads')
