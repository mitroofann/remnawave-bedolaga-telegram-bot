"""[Форк] поля для фичи «снятие сквадов при истечении подписки» (идемпотентно)

Добавляет:
- subscriptions.expire_disabled_squads (JSON, default []) — оригинальные сквады юзера,
  отложенные при истечении (для восстановления при продлении);
- subscriptions.expire_free_until (DateTime tz, nullable) — панельный expireAt на время
  free-окна (ветка B) и маркер, что free-окно активно;
- tariffs.expire_free_squads (JSON, default []) — free-сквады, выдаваемые при истечении;
- tariffs.expire_free_days (Integer, default 0) — сколько дней доступен free-сквад.

Изолированная фича форка (CUSTOM_EXPIRE_CLEAR_SQUADS_ENABLED). Миграция идемпотентна
(information_schema) — безопасна на проде, на свежей БД (bootstrap из моделей + stamp head)
не прогоняется. См. [[deferred-squad-limit-feature]], [[alembic-merge-revision-collision]].

Revision ID: 0107
Revises: 0106
"""

import sqlalchemy as sa
from alembic import op


revision = '0107'
down_revision = '0106'
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

    if not _has_column(conn, 'subscriptions', 'expire_disabled_squads'):
        op.add_column(
            'subscriptions',
            sa.Column('expire_disabled_squads', sa.JSON(), nullable=True, server_default='[]'),
        )
    if not _has_column(conn, 'subscriptions', 'expire_free_until'):
        op.add_column(
            'subscriptions',
            sa.Column('expire_free_until', sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column(conn, 'tariffs', 'expire_free_squads'):
        op.add_column(
            'tariffs',
            sa.Column('expire_free_squads', sa.JSON(), nullable=True, server_default='[]'),
        )
    if not _has_column(conn, 'tariffs', 'expire_free_days'):
        op.add_column(
            'tariffs',
            sa.Column('expire_free_days', sa.Integer(), nullable=False, server_default='0'),
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _has_column(conn, 'tariffs', 'expire_free_days'):
        op.drop_column('tariffs', 'expire_free_days')
    if _has_column(conn, 'tariffs', 'expire_free_squads'):
        op.drop_column('tariffs', 'expire_free_squads')
    if _has_column(conn, 'subscriptions', 'expire_free_until'):
        op.drop_column('subscriptions', 'expire_free_until')
    if _has_column(conn, 'subscriptions', 'expire_disabled_squads'):
        op.drop_column('subscriptions', 'expire_disabled_squads')
