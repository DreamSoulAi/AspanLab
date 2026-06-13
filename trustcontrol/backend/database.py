# ════════════════════════════════════════════════════════════
#  TrustControl — База данных
#  SQLAlchemy async + aiosqlite/asyncpg
# ════════════════════════════════════════════════════════════

from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from backend.config import settings


# ── Нормализация DATABASE_URL ────────────────────────────────
def _normalize_db_url(raw: str) -> tuple[str, dict]:
    """
    Приводит строку подключения к виду, который понимает async-движок,
    и собирает connect_args. Закрывает 3 грабли хостингов Postgres
    (Supabase / Neon / Render), на которых иначе падает с первого раза:

      1. Схема `postgres://` или `postgresql://` → `postgresql+asyncpg://`
         (async-движок работает только через asyncpg-драйвер).
      2. Параметр `?sslmode=...` (и `channel_binding`) asyncpg НЕ понимает —
         вырезаем из URL и переводим в connect_args ssl="require".
      3. Облачный Postgres требует SSL — включаем явно для не-localhost.

    Возвращает (нормализованный_url, connect_args).
    """
    url = (raw or "").strip()

    # SQLite — как было, ничего не трогаем кроме check_same_thread.
    if "sqlite" in url:
        return url, {"check_same_thread": False}

    # 1) Схема → asyncpg
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]

    if not url.startswith("postgresql+asyncpg://"):
        # Неизвестная СУБД (например MySQL) — отдаём как есть, без connect_args.
        return url, {}

    # 2) Вырезаем libpq-параметры, которые asyncpg не переваривает
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    sslmode = query.pop("sslmode", None)
    query.pop("channel_binding", None)
    clean_url = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )

    # 3) SSL для облака + отключаем кэш prepared statements
    #    (statement_cache_size=0 нужен если подключение идёт через
    #     pgbouncer в transaction-режиме — иначе ловим ошибки кэша).
    host = parts.hostname or ""
    is_local = host in ("localhost", "127.0.0.1", "::1", "")
    connect_args: dict = {"statement_cache_size": 0}
    if not is_local and sslmode != "disable":
        # ssl="require" = шифруем без строгой проверки сертификата —
        # работает на Supabase/Neon/Render с первого раза.
        connect_args["ssl"] = "require"

    return clean_url, connect_args


_DB_URL, _CONNECT_ARGS = _normalize_db_url(settings.DATABASE_URL)

# ── Движок ───────────────────────────────────────────────────
# Neon free даёт ~10 одновременных коннектов на проект.
# pool_size=3 постоянных + max_overflow=7 временных = 10 total.
# pool_recycle=300: переоткрываем коннект каждые 5 минут —
# Neon/облака рвут idle-соединения раньше чем SQLAlchemy замечает.
# SQLite не нуждается в этих параметрах (они просто игнорируются aiosqlite).
_is_postgres = "postgresql" in _DB_URL
engine = create_async_engine(
    _DB_URL,
    echo=settings.DEBUG,
    future=True,
    pool_pre_ping=True,
    pool_size=3          if _is_postgres else 5,
    max_overflow=7       if _is_postgres else 10,
    pool_recycle=300     if _is_postgres else -1,
    pool_timeout=30,
    connect_args=_CONNECT_ARGS,
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
    Создаём недостающие таблицы при старте (через create_all — IF NOT EXISTS).
    Изменения схемы существующих таблиц делаются через _fix_schema() в main.py.
    Данные НИКОГДА не уничтожаются автоматически.
    """
    import backend.models  # noqa — регистрирует все модели через __init__.py

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    print("✅ База данных инициализирована", flush=True)
    print(f"   URL: {settings.DATABASE_URL.split('@')[-1] if '@' in settings.DATABASE_URL else settings.DATABASE_URL}", flush=True)
