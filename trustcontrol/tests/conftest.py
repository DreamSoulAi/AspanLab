# ════════════════════════════════════════════════════════════
#  Тесты — конфигурация и фикстуры
#  pip install pytest pytest-asyncio httpx aiosqlite
#
#  Вход/самозапись — через Telegram Login Widget (см. helpers ниже).
# ════════════════════════════════════════════════════════════

import os

# Должно быть выставлено ДО импорта settings/main
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-ci-minimum-32-chars!!")
os.environ.setdefault("OPENAI_API_KEY", "sk-fake-key-for-tests")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake-token")

# Токен, которым подписываем тестовые данные Telegram-виджета
TG_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

import hashlib
import hmac
import itertools
import time

import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from main import app
from backend.database import Base, get_db
from backend.models import User, Location  # noqa — регистрация моделей


# ── Уникальные телефоны / telegram_id на всю сессию ──────────
# (чтобы тесты не пересекались, даже если БД одна)
_phone_counter = itertools.count(1)
_tg_counter    = itertools.count(1000)


def _next_phone() -> str:
    n = next(_phone_counter)
    return f"+7700000{n:04d}"


def _next_tg_id() -> int:
    return next(_tg_counter)


def telegram_widget_payload(
    tg_id: int | None = None,
    first_name: str = "Test",
    auth_date: int | None = None,
    bot_token: str = TG_BOT_TOKEN,
    sign: bool = True,
) -> dict:
    """
    Собирает тело запроса Telegram Login Widget с валидной HMAC-подписью
    (как это делает сам Telegram). sign=False → подпись битая (для негативных тестов).
    """
    payload: dict = {
        "id":         tg_id if tg_id is not None else _next_tg_id(),
        "first_name": first_name,
        "auth_date":  auth_date if auth_date is not None else int(time.time()),
    }
    check = "\n".join(sorted(f"{k}={v}" for k, v in payload.items()))
    secret = hashlib.sha256(bot_token.encode()).digest()
    payload["hash"] = (
        hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if sign else "deadbeef"
    )
    return payload


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
    tg_id: int | None = None,
    name: str = "Test User",
) -> dict:
    """
    Самозапись клиента через Telegram Login Widget.
    Возвращает {"access_token": ..., "user_id": ..., "tg_id": ..., ...}.
    """
    payload = telegram_widget_payload(tg_id=tg_id, first_name=name)
    r = await client.post("/api/auth/telegram-login", json=payload)
    assert r.status_code == 200, f"Telegram login failed: {r.text}"
    data = r.json()
    data["tg_id"] = payload["id"]
    return data


async def auth_headers(client: AsyncClient, tg_id: int | None = None) -> dict:
    """Самозапись клиента и возврат заголовков авторизации."""
    data = await register_user(client, tg_id=tg_id)
    return {"Authorization": f"Bearer {data['access_token']}"}
