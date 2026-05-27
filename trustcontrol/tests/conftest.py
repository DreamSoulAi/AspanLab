# ════════════════════════════════════════════════════════════
#  Тесты — конфигурация и фикстуры
#  pip install pytest pytest-asyncio httpx aiosqlite
#
#  OTP_BYPASS=true → код всегда 000000 (без отправки SMS).
# ════════════════════════════════════════════════════════════

import os

# Должно быть выставлено ДО импорта settings/main
os.environ.setdefault("OTP_BYPASS", "true")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-ci-minimum-32-chars!!")
os.environ.setdefault("OPENAI_API_KEY", "sk-fake-key-for-tests")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake-token")

import itertools

import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from main import app
from backend.database import Base, get_db
from backend.models import User, Location  # noqa — регистрация моделей


# ── Уникальные телефоны на всю сессию ────────────────────────
# (чтобы тесты не пересекались по номерам, даже если БД одна)
_phone_counter = itertools.count(1)


def _next_phone() -> str:
    n = next(_phone_counter)
    return f"+7700000{n:04d}"


# ── Фикстура: HTTP клиент со свежей БД на каждый тест ────────
@pytest_asyncio.fixture
async def client():
    """
    Свежая SQLite-in-memory БД и HTTP клиент на КАЖДЫЙ тест.
    Изоляция теста по БД и сбросе dependency overrides.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    session_maker = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def _override_get_db():
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # Сбрасываем in-memory rate limiter между тестами
    try:
        from backend.api.auth import _login_attempts
        _login_attempts.clear()
    except Exception:
        pass

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
    await engine.dispose()


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
