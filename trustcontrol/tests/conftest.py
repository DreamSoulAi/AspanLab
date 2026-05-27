# ════════════════════════════════════════════════════════════
#  Тесты — конфигурация и фикстуры
#  pip install pytest pytest-asyncio httpx aiosqlite
#
#  В CI: OTP_BYPASS=true → код всегда 000000 (без отправки SMS).
# ════════════════════════════════════════════════════════════

import os
# Должно быть выставлено ДО импорта settings/main
os.environ.setdefault("OTP_BYPASS", "true")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-ci-minimum-32-chars!!")
os.environ.setdefault("OPENAI_API_KEY", "sk-fake-key-for-tests")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake-token")

import asyncio
import itertools

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from main import app
from backend.database import Base, get_db
from backend.models import User, Location  # noqa — регистрация моделей


# ── Тестовая БД в памяти ─────────────────────────────────────
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DB_URL, echo=False)
TestSessionLocal = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Уникальные телефоны для изоляции тестов ──────────────────
# Каждый вызов register_user получает свой номер.
_phone_counter = itertools.count(1)


def _next_phone() -> str:
    n = next(_phone_counter)
    # +7 700 0000 NNNN — 11 цифр, валидный KZ формат
    return f"+7700000{n:04d}"


# ── Фикстура: общий event loop на сессию ─────────────────────
@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_db():
    """Создаём таблицы один раз на сессию."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client():
    """HTTP клиент с тестовой БД."""
    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# ── Вспомогательные функции ──────────────────────────────────

async def register_user(
    client: AsyncClient,
    phone: str | None = None,
    password: str = "testpass123",
    name: str = "Test User",
) -> dict:
    """
    Регистрирует пользователя и проходит OTP-верификацию.
    Возвращает {"access_token": ..., "phone": ..., "user_id": ...}.

    OTP_BYPASS=true в conftest → код всегда 000000.
    """
    phone = phone or _next_phone()

    r = await client.post("/api/auth/register", json={
        "name":     name,
        "phone":    phone,
        "password": password,
    })
    assert r.status_code == 200, f"Register failed: {r.text}"

    # В bypass-режиме сервер возвращает otp_code; верифицируем
    r = await client.post("/api/auth/verify-otp", json={
        "phone": phone,
        "code":  "000000",
    })
    assert r.status_code == 200, f"Verify OTP failed: {r.text}"
    data = r.json()
    data["phone"] = phone
    data["password"] = password
    return data


async def auth_headers(client: AsyncClient, phone: str | None = None) -> dict:
    """Регистрирует юзера и возвращает заголовки авторизации."""
    data = await register_user(client, phone=phone)
    return {"Authorization": f"Bearer {data['access_token']}"}
