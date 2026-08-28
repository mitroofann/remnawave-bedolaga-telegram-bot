"""
Регрессия: ``create_yookassa_payment`` обязан быть идемпотентным по
``yookassa_payment_id``, и вставка должна быть атомарной (без гонки
read-then-insert).

Рекуррентный автоплатёж (``recurrent_payment_service``) использует
детерминированный ключ идемпотентности на (подписку, карту, день), поэтому
повторные запуски шедулера в тот же день получают от YooKassa ТОТ ЖЕ
``payment_id``. Раньше повторная вставка била по unique-индексу
``ix_yookassa_payments_yookassa_payment_id`` и логировалась как ложный
«FK violation … user_id не существует» на уровне ERROR, заваливая админ-чат
(прод-отчёт от 01.06.2026, payment 31aee339-000f-5000-b000-1d3e737c34d1).

[Форк 2026-08-28] Реализация переведена на ``INSERT ... ON CONFLICT DO
NOTHING ... RETURNING``:
  * идемпотентность обеспечивает сам конфликтный апдейт, а не read-then-insert;
  * гонка «два одинаковых payment_id одновременно» больше не порождает
    дедлок на FOR KEY SHARE-локе строки users (см. handle_webhook в
    yookassa_webhook.py).
"""

from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.dialects.postgresql import Insert

from app.database.crud import yookassa as yk


def _result(value):
    """Мок результата ``db.execute``: ``.scalar_one_or_none()`` (ORM-returning)."""
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=value)
    return res


def _db(execute_returns):
    """Мок AsyncSession; ``db.execute`` последовательно отдаёт переданные значения."""
    db = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(v) for v in execute_returns])
    return db


async def test_returns_existing_when_on_conflict():
    """Платеж c таким payment_id уже есть → INSERT возвращает пусто (конфликт),
    возвращаем существующую запись, без лишнего peer-чтения в пре-чеке."""
    existing = MagicMock(yookassa_payment_id='dup-1')
    # 1) ON CONFLICT ... RETURNING не вставил (конфликт) → None
    # 2) доводочное чтение уже существующей записи → existing
    db = _db([None, existing])

    result = await yk.create_yookassa_payment(
        db=db,
        user_id=607,
        yookassa_payment_id='dup-1',
        amount_kopeks=7420,
        currency='RUB',
        description='RichVPN',
        status='canceled',
    )

    assert result is existing
    db.add.assert_not_called()
    db.refresh.assert_not_awaited()
    # Ровно один INSERT-выполнение (атомарный upsert) + один добирающий SELECT.
    assert db.execute.await_count == 2


async def test_inserts_when_new():
    """Записи нет → INSERT вставляет (RETURNING отдаёт объект), коммитим,
    возвращаем новый платёж."""
    new_payment = MagicMock(yookassa_payment_id='new-1')
    db = _db([new_payment])

    result = await yk.create_yookassa_payment(
        db=db,
        user_id=607,
        yookassa_payment_id='new-1',
        amount_kopeks=7420,
        currency='RUB',
        description='RichVPN',
        status='pending',
    )

    db.commit.assert_awaited_once()
    assert result is new_payment
    assert result.yookassa_payment_id == 'new-1'


async def test_atomic_upsert_uses_on_conflict():
    """Строка для INSERT обязана быть атомарной: pg_insert + on_conflict_do_nothing."""
    db = _db([None, None])

    await yk.create_yookassa_payment(
        db=db,
        user_id=607,
        yookassa_payment_id='atomic-1',
        amount_kopeks=100,
        currency='RUB',
        description='RichVPN',
        status='pending',
    )

    # Первое (и единственное) выполнение — это атомарный upsert, а не SELECT пре-чек.
    stmt = db.execute.await_args_list[0].args[0]
    assert isinstance(stmt, Insert)
    # ON CONFLICT DO NOTHING зашит в пост-клаузу стейтмента (OnConflictDoNothing),
    # RETURNING тоже настроен — это и есть «атомарная идемпотентная вставка».
    assert type(stmt._post_values_clause).__name__ == 'OnConflictDoNothing'
    assert stmt._returning is not None


async def test_race_between_two_creates_is_serialised():
    """Имитация атомарного upsert: два конкурента с одним payment_id. Первый
    вставляет, второй получает конфликт и возвращает запись первого."""
    winner = MagicMock(yookassa_payment_id='race-1', user_id=607)

    # Конкурент A: единственный INSERT "выиграл" — RETURNING отдал запись.
    db_a = _db([winner])
    result_a = await yk.create_yookassa_payment(
        db=db_a,
        user_id=607,
        yookassa_payment_id='race-1',
        amount_kopeks=100,
        currency='RUB',
        description='RichVPN',
        status='pending',
    )
    assert result_a is winner

    # Конкурент B: INSERT получил конфликт (None) → читает и возвращает запись A.
    db_b = _db([None, winner])
    result_b = await yk.create_yookassa_payment(
        db=db_b,
        user_id=607,
        yookassa_payment_id='race-1',
        amount_kopeks=100,
        currency='RUB',
        description='RichVPN',
        status='pending',
    )
    assert result_b is winner