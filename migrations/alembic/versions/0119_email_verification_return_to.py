"""return_to в письме подтверждения email

Revision ID: 0119
Revises: 0109_marketing_bots
Create Date: 2026-09-09

Лендинг продаёт через двухшаговый флоу: регистрация по email → ссылка из письма
открывается в новом табе, где sessionStorage фронта пуст, и человек выпадает из
покупки. Путь возврата теперь едет в самой ссылке (`&return_to=<path>`) —
значит, его надо где-то держать от регистрации до клика. Живёт рядом с токеном
подтверждения в той же строке users, очищается при верификации.

NB: down='0109_marketing_bots', а не '0118' — это голова форк-стека, перецепленного
в хвост апстрим-линейки при мерже 4.7.0 (см. 0104_landing_trial, NB3). Новая форковая
ревизия продолжается от него же, иначе две головы.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0119'
down_revision: Union[str, None] = '0109_marketing_bots'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return True  # таблицы нет — создастся уже с колонкой
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if _has_column('users', 'email_verification_return_to'):
        return
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('email_verification_return_to', sa.String(512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.drop_column('email_verification_return_to')
