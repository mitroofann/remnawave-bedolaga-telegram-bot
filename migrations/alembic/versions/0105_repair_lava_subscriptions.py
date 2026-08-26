"""repair: досоздать lava_subscriptions + tariffs.lava_product_id (идемпотентно)

Почему нужна отдельная repair-миграция, а не правка 0101_add_lava_subscriptions:
при мерже upstream/main в форк случилась КОЛЛИЗИЯ Alembic-ревизий. Форк уже был
задеплоен с собственной миграцией revision='0101' (landing-trial), поэтому в проде
alembic_version стоял на 0101. Апстрим принёс СВОЮ 0101 (lava_subscriptions).
Форковая landing-trial была перепривязана в хвост (0104), но alembic_version в
проде уже = 0101 → `upgrade head` счёл ревизию 0101 применённой и ПРОПУСТИЛ
создание таблицы lava_subscriptions. Итог: _reconcile_lava_subscriptions в
мониторинге падал на несуществующей таблице и отравлял транзакцию (каскад
InFailedSQLTransactionError по всем задачам цикла).

Эта миграция идемпотентно досоздаёт ровно то, что могло быть пропущено. Безопасна:
- на проде форка (таблицы нет) — создаёт;
- на свежей БД (bootstrap из моделей + stamp head) — не прогоняется вовсе;
- на БД, где lava_subscriptions уже есть (напр. чистый апстрим) — всё пропускает.
Апстримовые файлы 0101/0102/0103 НЕ трогаем — меньше конфликтов при будущих мержах.

Revision ID: 0105
Revises: 0104_landing_trial
"""

import sqlalchemy as sa
from alembic import op

revision = '0105_repair_lava_subscriptions'
down_revision = '0104_landing_trial'
branch_labels = None
depends_on = None

_ALIVE = "('PENDING', 'ACTIVE', 'PAST_DUE')"


def _has_table(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                'SELECT EXISTS (SELECT 1 FROM information_schema.tables '
                'WHERE table_name = :tbl)'
            ),
            {'tbl': table},
        ).scalar()
    )


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

    if not _has_table(conn, 'lava_subscriptions'):
        op.create_table(
            'lava_subscriptions',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column(
                'subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='CASCADE'), nullable=False
            ),
            sa.Column('tariff_id', sa.Integer(), sa.ForeignKey('tariffs.id'), nullable=True),
            sa.Column('lava_subscription_id', sa.String(length=255), nullable=True),
            sa.Column('lava_product_id', sa.String(length=255), nullable=False),
            sa.Column('lava_consumer_id', sa.String(length=255), nullable=True),
            sa.Column('order_id', sa.String(length=255), nullable=False),
            sa.Column('charge_days', sa.Integer(), nullable=False),
            sa.Column('amount_kopeks', sa.Integer(), nullable=False),
            sa.Column('currency', sa.String(length=10), nullable=False, server_default='RUB'),
            sa.Column('status', sa.String(length=20), nullable=False, server_default='PENDING'),
            sa.Column('redirect_url', sa.Text(), nullable=True),
            sa.Column('next_charge_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_charge_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_charge_external_id', sa.String(length=255), nullable=True),
            sa.Column('charges_success', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('charges_failed', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index('ix_lava_subscriptions_user_id', 'lava_subscriptions', ['user_id'])
        op.create_index('ix_lava_subscriptions_subscription_id', 'lava_subscriptions', ['subscription_id'])
        op.create_unique_constraint('uq_lava_subscriptions_lava_id', 'lava_subscriptions', ['lava_subscription_id'])
        op.create_unique_constraint('uq_lava_subscriptions_order_id', 'lava_subscriptions', ['order_id'])
        op.create_index('ix_lava_subscriptions_user_active', 'lava_subscriptions', ['user_id', 'status'])

    # Partial unique index уже IF NOT EXISTS — безопасно вызывать всегда.
    op.execute(
        sa.text(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_lava_subscriptions_alive
            ON lava_subscriptions (subscription_id)
            WHERE status IN {_ALIVE}
            """
        )
    )

    if not _has_column(conn, 'tariffs', 'lava_product_id'):
        op.add_column('tariffs', sa.Column('lava_product_id', sa.String(length=255), nullable=True))


def downgrade() -> None:
    # Repair-миграция ничего не «изобретает» сверх 0101_add_lava_subscriptions,
    # поэтому откат отдаём той миграции. Здесь — no-op, чтобы downgrade -1 с 0105
    # не дропал таблицу, созданную (логически) ревизией 0101.
    pass
