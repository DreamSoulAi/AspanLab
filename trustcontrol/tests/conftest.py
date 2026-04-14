# ════════════════════════════════════════════════════════════
#  Тесты — конфигурация и фикстуры
#  pip install pytest pytest-asyncio httpx
# ════════════════════════════════════════════════════════════

import pytest
import asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from main import app
from backend.database import Base, get_db
from backend.models import User, Location  # noqa

# ── Тестовая БД в памяти ─────────────────────────────────────
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DB_URL, echo=False)
TestSessionLocal = sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Фикстура: приложение с тестовой БД ───────────────────────
@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def setup_db():
    """Создаём таблицы в тестовой БД."""
    import backend.models  # noqa — регистрация моделей
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client(setup_db):
    """HTTP клиент с тестовой БД."""
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# ── Вспомогательные функции ──────────────────────────────────

async def register_user(client: AsyncClient, email: str = "test@test.com") -> dict:
    """Регистрируем тестового пользователя, возвращаем токен."""
    r = await client.post("/api/auth/register", json={
        "name": "Test User",
        "email": email,
        "phone": "+77001234567",
        "password": "testpass123",
    })
    assert r.status_code == 200, f"Register failed: {r.text}"
    return r.json()


async def auth_headers(client: AsyncClient, email: str = "test@test.com") -> dict:
    """Возвращаем заголовки авторизации."""
    data = await register_user(client, email)
    return {"Authorization": f"Bearer {data['access_token']}"}
