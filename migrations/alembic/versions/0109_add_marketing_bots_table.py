"""Add marketing_bots table for standalone marketing telegram bots.

Revision ID: 0109
Revises: 0108
"""

import sqlalchemy as sa
from alembic import op


revision = '0109'
down_revision = '0108'
branch_labels = None
depends_on = None


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


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_table(conn, 'marketing_bots'):
        op.create_table(
            'marketing_bots',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=255), nullable=False),
            sa.Column('bot_token', sa.String(length=255), nullable=False),
            sa.Column('welcome_message', sa.Text(), nullable=False),
            sa.Column('image_url', sa.String(length=512), nullable=True),
            sa.Column('button_text', sa.String(length=100), nullable=True),
            sa.Column('button_url', sa.String(length=512), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_marketing_bots_id', 'marketing_bots', ['id'])
        op.create_index('ix_marketing_bots_is_active', 'marketing_bots', ['is_active'])


def downgrade() -> None:
    conn = op.get_bind()

    if _has_table(conn, 'marketing_bots'):
        op.drop_index('ix_marketing_bots_is_active', 'marketing_bots')
        op.drop_index('ix_marketing_bots_id', 'marketing_bots')
        op.drop_table('marketing_bots')
