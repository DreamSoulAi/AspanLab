# ╔══════════════════════════════════════════════════════════╗
# ║              TrustControl — Главный файл  (v3.0)         ║
# ║         Запускает API сервер для всех точек               ║
# ╚══════════════════════════════════════════════════════════╝

import asyncio
import logging
import os
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from backend.api.locations import router as locations_router
from backend.api.reports    import router as reports_router
from backend.api.alerts     import router as alerts_router
from backend.api.auth       import router as auth_router
from backend.api.stats      import router as stats_router
from backend.api.pos              import router as pos_router
from backend.api.health           import router as health_router
from backend.api.summary          import router as summary_router
from backend.api.incidents        import router as incidents_router
from backend.api.telegram_webhook import router as tg_router
from backend.database             import init_db, AsyncSessionLocal
from backend.config               import settings

log = logging.getLogger("main")

app = FastAPI(
    title="TrustControl API",
    description="ИИ-мониторинг качества обслуживания",
    version="3.0.0",
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url=None,
)

# ── CORS ─────────────────────────────────────────────────────
ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8000"
).split(",")
if settings.DEBUG:
    ALLOWED_ORIGINS = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

# ── API роуты ────────────────────────────────────────────────
app.include_router(auth_router,      prefix="/api/auth",        tags=["Auth"])
app.include_router(locations_router, prefix="/api/locations",   tags=["Locations"])
app.include_router(reports_router,   prefix="/api/reports",     tags=["Reports"])
app.include_router(alerts_router,    prefix="/api/alerts",      tags=["Alerts"])
app.include_router(stats_router,     prefix="/api/stats",       tags=["Stats"])
app.include_router(pos_router,       prefix="/api/v1/pos",       tags=["POS"])
app.include_router(health_router,    prefix="/api/v1/health",    tags=["Health"])
app.include_router(summary_router,   prefix="/api/v1/summary",   tags=["Summary"])
app.include_router(incidents_router, prefix="/api/v1/incidents", tags=["Incidents"])
app.include_router(tg_router,        prefix="/telegram",         tags=["Telegram"])

# ── Фронтенд ─────────────────────────────────────────────────
DASHBOARD_DIR = Path(__file__).parent / "frontend" / "dashboard"
if DASHBOARD_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(DASHBOARD_DIR), html=True), name="dashboard")

    @app.get("/")
    async def root():
        return FileResponse(str(DASHBOARD_DIR / "index.html"))


# ── Фоновые задачи ───────────────────────────────────────────

async def _retry_worker():
    """
    Каждые 5 минут забирает FailedJob из очереди и повторяет обработку.
    Максимум 3 попытки, после — статус failed_permanently.
    """
    from sqlalchemy import select, update as sql_update
    from backend.database import AsyncSessionLocal
    from backend.models.failed_job import FailedJob
    from backend.api.reports import _process_submission
    from datetime import datetime

    MAX_RETRIES = 3
    while True:
        await asyncio.sleep(300)   # 5 минут
        try:
            async with AsyncSessionLocal() as db:
                now = datetime.utcnow()
                result = await db.execute(
                    select(FailedJob).where(
                        FailedJob.status       == "pending",
                        FailedJob.next_retry_at <= now,
                    ).limit(10)
                )
                jobs = result.scalars().all()

            for job in jobs:
                wav_bytes = None
                if job.audio_path:
                    p = Path(job.audio_path)
                    if p.exists():
                        wav_bytes = p.read_bytes()

                if not wav_bytes and not job.transcript_text:
                    async with AsyncSessionLocal() as db2:
                        j = await db2.get(FailedJob, job.id)
                        if j:
                            j.status = "failed_permanently"
                            await db2.commit()
                    continue

                log.info(f"Повтор FailedJob #{job.id} (попытка {job.retry_count + 1})")
                await _process_submission(
                    location_id=job.location_id,
                    wav_bytes=wav_bytes,
                    transcript_text=job.transcript_text,
                    language=job.language,
                    audio_size_kb=job.audio_size_kb,
                    business_type=job.business_type,
                    custom_phrases=job.custom_phrases or [],
                    telegram_chat=job.telegram_chat,
                    location_name=job.location_name,
                    failed_job_id=job.id,
                )

                # Если _process_submission не удалил job — увеличиваем счётчик
                async with AsyncSessionLocal() as db3:
                    j = await db3.get(FailedJob, job.id)
                    if j:
                        if j.retry_count >= MAX_RETRIES - 1:
                            j.status = "failed_permanently"
                            log.warning(f"FailedJob #{job.id} — превышен лимит попыток")
                        else:
                            j.retry_count    += 1
                            j.next_retry_at   = datetime.utcnow() + timedelta(minutes=5)
                        await db3.commit()

        except Exception as e:
            log.error(f"Retry worker ошибка: {e}")


async def _retention_worker():
    """Каждые 4 часа запускает S3 retention policy."""
    from backend.services.retention import run_retention
    while True:
        await asyncio.sleep(4 * 3600)
        try:
            await run_retention()
        except Exception as e:
            log.error(f"Retention worker ошибка: {e}")


async def _daily_report_worker():
    """
    Каждый день в 22:00 (UTC+5 = 17:00 UTC) отправляет вечерний отчёт
    всем владельцам у которых есть telegram_chat.
    """
    from sqlalchemy import select, func
    from backend.database import AsyncSessionLocal
    from backend.models.user import User
    from backend.models.location import Location
    from backend.models.report import Report
    from backend.services import notifier
    from datetime import datetime, timedelta
    import asyncio

    TARGET_HOUR_UTC = 17   # 22:00 Алматы (UTC+5)

    while True:
        now = datetime.utcnow()
        # Следующий запуск в TARGET_HOUR_UTC:00
        next_run = now.replace(hour=TARGET_HOUR_UTC, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        await asyncio.sleep((next_run - now).total_seconds())

        try:
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            async with AsyncSessionLocal() as db:
                users = await db.execute(
                    select(User).where(User.telegram_chat != None, User.is_active == True)
                )
                for user in users.scalars().all():
                    locs = await db.execute(
                        select(Location).where(Location.owner_id == user.id, Location.is_active == True)
                    )
                    for loc in locs.scalars().all():
                        if not user.telegram_chat:
                            continue
                        # Считаем статистику за день
                        reps = await db.execute(
                            select(Report).where(
                                Report.location_id == loc.id,
                                Report.timestamp   >= today_start,
                                Report.is_hidden   == False,
                            )
                        )
                        rows = reps.scalars().all()
                        total = len(rows)
                        if total == 0:
                            continue

                        upsell_count   = sum(1 for r in rows if r.upsell_attempt)
                        greeting_count = sum(1 for r in rows if r.has_greeting)
                        neg_count      = sum(1 for r in rows if r.tone == "negative")
                        fraud_risks    = sum(1 for r in rows if r.fraud_status == "critical_fraud_risk")
                        sat_scores     = [r.customer_satisfaction for r in rows if r.customer_satisfaction]
                        avg_sat        = (sum(sat_scores) / len(sat_scores)) if sat_scores else 0.0

                        await notifier.send_daily_summary(
                            chat_id=user.telegram_chat,
                            location_name=loc.name,
                            stats={
                                "total":         total,
                                "upsell_count":  upsell_count,
                                "upsell_pct":    upsell_count / total * 100,
                                "avg_satisfaction": avg_sat,
                                "fraud_risks":   fraud_risks,
                                "negative_count": neg_count,
                                "greeting_pct":  greeting_count / total * 100,
                            },
                        )
        except Exception as e:
            log.error(f"Daily report worker ошибка: {e}")


# ── Lifecycle ─────────────────────────────────────────────────

async def _run_alembic():
    """
    Run Alembic migrations at startup.
    If DB already has tables but no alembic_version (pre-Alembic deploy),
    stamp as head instead of running migrations — avoids "table already exists" crash.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    def _upgrade():
        from alembic.config import Config
        from alembic import command
        from pathlib import Path
        import sqlalchemy as sa

        cfg = Config(str(Path(__file__).parent / "alembic.ini"))
        cfg.set_main_option("script_location", str(Path(__file__).parent / "alembic"))

        # Build a sync DSN for the check query
        dsn = settings.DATABASE_URL \
            .replace("postgresql+asyncpg://", "postgresql://") \
            .replace("sqlite+aiosqlite://", "sqlite://")

        engine = sa.create_engine(dsn)
        try:
            with engine.connect() as conn:
                has_alembic = sa.inspect(engine).has_table("alembic_version")
                has_users   = sa.inspect(engine).has_table("users")
        finally:
            engine.dispose()

        if has_users and not has_alembic:
            # Pre-Alembic database: mark all migrations as already applied
            command.stamp(cfg, "head")
            print("✅ Alembic: stamped existing DB as head (no migrations run)")
        else:
            # Fresh DB or already Alembic-managed: run normally
            command.upgrade(cfg, "head")
            print("✅ Alembic migrations applied")

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:
        await loop.run_in_executor(pool, _upgrade)


@app.on_event("startup")
async def startup():
    await _run_alembic()

    # Запускаем фоновые задачи
    asyncio.create_task(_retry_worker())
    asyncio.create_task(_retention_worker())
    asyncio.create_task(_daily_report_worker())

    from backend.services.health_monitor import run_health_monitor
    asyncio.create_task(run_health_monitor())

    print("✅ TrustControl API v3.0 запущен!")
    print(f"📡 http://localhost:{settings.PORT}")
    if settings.DEBUG:
        print(f"📖 Документация: http://localhost:{settings.PORT}/docs")


@app.on_event("shutdown")
async def shutdown():
    print("👋 TrustControl API остановлен")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "3.0.0"}


if __name__ == "__main__":
    from datetime import timedelta  # нужен в _retry_worker closure
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level="info",
    )
