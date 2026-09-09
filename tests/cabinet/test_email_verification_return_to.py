"""return_to в письме подтверждения email — возврат на лендинг после верификации.

Лендинг продаёт через двухшаговый флоу: регистрация по email → «Проверьте почту» →
ссылка из письма открывается в новом табе, где sessionStorage фронта пуст, и
человек выпадает из покупки в кабинет. Путь возврата теперь едет в самой ссылке
(`&return_to=<path>`), поэтому бэкенд принимает его при регистрации, хранит рядом
с токеном подтверждения и подставляет в письмо. Значение приходит от клиента,
а письмо — исходящий редирект, поэтому всё, что не похоже на путь от корня,
тихо отбрасывается (open-redirect через письмо).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.cabinet.auth import email_auth_gate as gate
from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.routes.auth import router as auth_router
from app.database.models import AdminRole, CabinetRefreshToken, PromoGroup, SystemSetting, User, UserRole
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    SystemSetting.__table__,
    PromoGroup.__table__,
    User.__table__,
    AdminRole.__table__,
    UserRole.__table__,
    CabinetRefreshToken.__table__,
)

REGISTER_URL = '/cabinet/auth/email/register/standalone'
RESEND_URL = '/cabinet/auth/email/register/resend'
VERIFY_URL = '/cabinet/auth/email/verify'


def _app(db) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router, prefix='/cabinet')

    async def _db():
        yield db

    app.dependency_overrides[get_cabinet_db] = _db
    return app


def _arm(monkeypatch) -> MagicMock:
    """Верификация включена, почта настроена, дроссель и переопределения шаблонов выключены."""
    from app.cabinet.auth import registration_throttle as throttle
    from app.cabinet.routes import auth

    monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', True)
    monkeypatch.setattr(auth.settings, 'CABINET_EMAIL_VERIFICATION_ENABLED', True)
    monkeypatch.setattr(auth.settings, 'CABINET_URL', 'https://cabinet.example.org')
    # Гейт согласий с документами в standalone-флоу выключен: проверяется в своих тестах.
    monkeypatch.setattr(auth.settings, 'CABINET_REQUIRE_LEGAL_CONSENT', False)

    async def throttle_ip(_ip: str, _action: str, limit: int, window: int, *, fail_closed: bool = False):
        return False

    async def no_registration_gate(*_args, **_kwargs):
        return None

    async def registration_throttle(*_args) -> None:
        return None

    async def no_override(*_args, **_kwargs):
        return None

    monkeypatch.setattr(throttle.RateLimitCache, 'is_ip_rate_limited', throttle_ip)
    monkeypatch.setattr(auth, 'enforce_email_registration_throttle', registration_throttle)
    monkeypatch.setattr(auth, 'enforce_verification_resend_throttle', registration_throttle)
    monkeypatch.setattr(auth, 'get_rendered_override', no_override)

    sender = MagicMock(return_value=True)
    monkeypatch.setattr(auth.email_service, 'is_configured', MagicMock(return_value=True))
    monkeypatch.setattr(auth.email_service, 'send_verification_email', sender)
    return sender


async def _user_of(db, email: str) -> User:
    user = (await db.execute(select(User).where(User.email == email))).scalar_one()
    await db.refresh(user)
    return user


# ==================== приём return_to при регистрации ====================


@pytest.mark.asyncio
async def test_registration_stores_return_to(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sender = _arm(monkeypatch)

        async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://cabinet') as client:
            response = await client.post(
                REGISTER_URL,
                json={
                    'email': 'ivan@example.org',
                    'password': '12345678',
                    'return_to': '/buy/my-landing/flow?intent=trial',
                },
            )

        assert response.status_code == 200, response.text
        assert (await _user_of(db, 'ivan@example.org')).email_verification_return_to == (
            '/buy/my-landing/flow?intent=trial'
        )
        # Ссылку в письме собирает email_service: `?` и `&` внутри пути должны
        # приехать urlencoded, чтобы не сломать внешнюю ссылку.
        assert sender.call_args.kwargs['return_to'] == '/buy/my-landing/flow?intent=trial'


@pytest.mark.asyncio
async def test_registration_without_return_to_keeps_letter_untouched(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sender = _arm(monkeypatch)

        async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://cabinet') as client:
            response = await client.post(REGISTER_URL, json={'email': 'ivan@example.org', 'password': '12345678'})

        assert response.status_code == 200, response.text
        assert (await _user_of(db, 'ivan@example.org')).email_verification_return_to is None
        kwargs = sender.call_args.kwargs
        assert 'return_to=' not in str(kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'bad_value',
    [
        'https://evil.com/x',
        '//evil.com',
        '/x\n',
        '',
        'relative/path',
        '/a\\b',
        ':8080/x',
        'x' * 513,
    ],
)
async def test_unsafe_return_to_is_dropped_silently(monkeypatch, bad_value):
    async with memory_session(monkeypatch, TABLES) as db:
        sender = _arm(monkeypatch)

        async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://cabinet') as client:
            response = await client.post(
                REGISTER_URL,
                json={'email': 'ivan@example.org', 'password': '12345678', 'return_to': bad_value},
            )

        assert response.status_code == 200, response.text
        assert (await _user_of(db, 'ivan@example.org')).email_verification_return_to is None
        assert 'return_to=' not in str(sender.call_args.kwargs)


# ==================== resend переиспользует сохранённый путь ====================


@pytest.mark.asyncio
async def test_resend_reuses_saved_return_to(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sender = _arm(monkeypatch)

        async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://cabinet') as client:
            await client.post(
                REGISTER_URL,
                json={'email': 'ivan@example.org', 'password': '12345678', 'return_to': '/buy/x/flow'},
            )
            first_call = sender.call_args
            sender.reset_mock()
            response = await client.post(RESEND_URL, json={'email': 'ivan@example.org'})

        assert response.status_code == 200, response.text
        assert sender.call_count == 1
        assert sender.call_args.kwargs['return_to'] == first_call.kwargs['return_to'] == '/buy/x/flow'


# ==================== верификация чистит путь ====================


@pytest.mark.asyncio
async def test_verification_clears_return_to(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _arm(monkeypatch)

        async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://cabinet') as client:
            await client.post(
                REGISTER_URL,
                json={'email': 'ivan@example.org', 'password': '12345678', 'return_to': '/buy/x/flow'},
            )
            user = await _user_of(db, 'ivan@example.org')
            response = await client.post(VERIFY_URL, json={'token': user.email_verification_token})

        assert response.status_code == 200, response.text
        assert (await _user_of(db, 'ivan@example.org')).email_verification_return_to is None


# ==================== валидатор схемы ====================


def test_validator_normalization_table():
    from app.cabinet.schemas.auth import EmailRegisterStandaloneRequest

    cases = {
        '/buy/my-landing/flow?intent=trial': '/buy/my-landing/flow?intent=trial',
        '/cabinet': '/cabinet',
        'https://evil.com/x': None,
        '//evil.com': None,
        '/x\n': None,
        '': None,
        'relative/path': None,
        None: None,
        '/a\\b': None,
        ':8080/x': None,
        'x' * 513: None,
        '/' + 'x' * 511: '/' + 'x' * 511,
    }
    for value, expected in cases.items():
        got = EmailRegisterStandaloneRequest(
            email='a@b.co', password='12345678', return_to=value
        ).return_to
        assert got == expected, f'{value!r} -> {got!r}, expected {expected!r}'


# ==================== сборка ссылки в email_service ====================


def test_service_appends_urlencoded_return_to(monkeypatch):
    """`?` и `&` внутри пути кодируются, ссылка без return_to не меняется."""
    from app.cabinet.services.email_service import EmailService

    captured: dict = {}

    def fake_render(self, notification_type, language, context):
        captured['context'] = context
        return ('subject', '<html></html>')

    sent: dict = {}

    def fake_send(self, to_email, subject, body_html, retry_until=None):
        sent['body'] = body_html
        return True

    monkeypatch.setattr(EmailService, '_render_default_template', fake_render)
    monkeypatch.setattr(EmailService, 'send_email', fake_send)

    service = EmailService()
    assert service.send_verification_email(
        to_email='a@b.co',
        verification_token='tok123',
        verification_url='https://cabinet.example.org/verify-email',
        return_to='/buy/my-landing/flow?intent=trial',
    )
    assert captured['context']['verification_url'] == (
        'https://cabinet.example.org/verify-email?token=tok123'
        '&return_to=%2Fbuy%2Fmy-landing%2Fflow%3Fintent%3Dtrial'
    )


def test_service_without_return_to_keeps_link_unchanged(monkeypatch):
    from app.cabinet.services.email_service import EmailService

    captured: dict = {}

    def fake_render(self, notification_type, language, context):
        captured['context'] = context
        return ('subject', '<html></html>')

    monkeypatch.setattr(EmailService, '_render_default_template', fake_render)
    monkeypatch.setattr(EmailService, 'send_email', lambda self, *a, **k: True)

    service = EmailService()
    assert service.send_verification_email(
        to_email='a@b.co',
        verification_token='tok123',
        verification_url='https://cabinet.example.org/verify-email',
    )
    assert captured['context']['verification_url'] == 'https://cabinet.example.org/verify-email?token=tok123'
