# ════════════════════════════════════════════════════════════
#  TrustControl — База данных
#  SQLAlchemy async + aiosqlite/asyncpg
# ════════════════════════════════════════════════════════════

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from backend.config import settings

# ── Движок ───────────────────────────────────────────────────
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,   # SQL логи только в dev
    future=True,
    # Для SQLite — нужен check_same_thread=False
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
)

# ── Фабрика сессий ───────────────────────────────────────────
AsyncSessionLocal = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()


# ── Dependency для FastAPI ───────────────────────────────────
async def get_db():
    """Инжектируется в каждый API endpoint."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ── Инициализация таблиц ─────────────────────────────────────
async def init_db():
    """
    Создаём все таблицы при старте.
    Модели импортируются через backend.models для регистрации в Base.metadata
    """
    import backend.models  # noqa — регистрирует все модели через __init__.py

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    print("✅ База данных инициализирована")
    print(f"   URL: {settings.DATABASE_URL.split('@')[-1] if '@' in settings.DATABASE_URL else settings.DATABASE_URL}")
