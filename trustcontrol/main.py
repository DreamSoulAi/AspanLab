# ╔═══════════════════════════════════════════════════════════╗
# ║              TrustControl — Главный файл  (v3.0)         ║
# ║         Запускает API сервер для всех точек               ║
# ╚═══════════════════════════════════════════════════════════╝

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
from backend.api.download         import router as download_router
from backend.api.monitor_update   import router as monitor_router
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

# ── CORS ────────────────────────────────────────────
# В проде: только домены из ALLOWED_ORIGINS env (через запятую).
# В DEBUG: добавляем localhost для разработки, но НЕ wildcard.
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:3000,http://localhost:8000"
    ).split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

# ── API роуты ────────────────────────────────────────
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
app.include_router(download_router,  prefix="/api/download",     tags=["Download"])
app.include_router(monitor_router,   prefix="/api/monitor",      tags=["Monitor"])

# ── Фронтенд ─────────────────────────────────────────
DASHBOARD_DIR = Path(__file__).parent / "frontend" / "dashboard"
if DASHBOARD_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(DASHBOARD_DIR), html=True), name="dashboard")

    @app.get("/")
    async def root():
        resp = FileResponse(str(DASHBOARD_DIR / "index.html"))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp

# ── PWA-микрофон (для Android/iPhone вместо .exe) ──────
MIC_DIR = Path(__file__).parent / "frontend" / "mic"
if MIC_DIR.exists():
    app.mount("/mic", StaticFiles(directory=str(MIC_DIR), html=True), name="mic")

    @app.get("/app-mic-manifest.json")
    async def mic_manifest():
        from fastapi.responses import JSONResponse
        return JSONResponse({
            "name": "TrustControl Mic",
            "short_name": "TC Mic",
            "start_url": "/mic/",
            "scope": "/mic/",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#0a0a0a",
            "theme_color": "#0a0a0a",
            "icons": [
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
        })


# ── Фоновые задачи ───────────────────────────────────

async def _retry_worker():
    """
    Каждые 5 минут забирает FailedJob из очереди и повторяет обработку.
    Максимум 3 попытки, после — статус failed_permanently.
    """
    from sqlalchemy import select, update as sql_update
    from backend.database import AsyncSessionLocal
    from backend.models.failed_job import FailedJob
    from backend.api.reports import _process_submission
    from datetime import datetime, timedelta

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
                # Берём всех активных пользователей (не только с telegram_chat)
                users = await db.execute(
                    select(User).where(User.is_active == True)
                )
                for user in users.scalars().all():
                    locs = await db.execute(
                        select(Location).where(Location.owner_id == user.id, Location.is_active == True)
                    )
                    for loc in locs.scalars().all():
                        # Приоритет: telegram_chat точки, затем telegram_chat пользователя
                        chat_id = loc.telegram_chat or user.telegram_chat
                        if not chat_id:
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
                            chat_id=chat_id,
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


# ── Lifecycle ─────────────────────────────────────────────

async def _run_alembic():
    """
    Run Alembic migrations at startup — PostgreSQL only.
    For SQLite, init_db() create_all handles schema directly.
    """
    if "sqlite" in settings.DATABASE_URL:
        log.info("SQLite detected — пропускаем Alembic, используем create_all")
        return

    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    def _upgrade():
        from alembic.config import Config
        from alembic import command
        from pathlib import Path

        cfg = Config(str(Path(__file__).parent / "alembic.ini"))
        cfg.set_main_option("script_location", str(Path(__file__).parent / "alembic"))
        command.upgrade(cfg, "head")
        print("✅ Alembic migrations applied")

    try:
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            await loop.run_in_executor(pool, _upgrade)
    except Exception as e:
        log.error(f"⚠️ Alembic ошибка (не критично, продолжаем): {e}")


async def _fix_schema():
    """
    Direct SQL safety net — runs EVERY startup after Alembic.
    Each step is isolated — one failure never blocks the rest.
    """
    import sqlalchemy as sa
    from backend.database import AsyncSessionLocal

    is_pg = "postgresql" in settings.DATABASE_URL or settings.DATABASE_URL.startswith("postgres")

    async def _run(db, sql: str, msg: str):
        try:
            await db.execute(sa.text(sql))
            await db.commit()
            print(f"✅ schema fix: {msg}", flush=True)
        except Exception as e:
            await db.rollback()
            print(f"⚠️ schema fix [{msg}]: {e}", flush=True)

    async def _check_col(db, table: str, col: str) -> bool:
        r = await db.execute(sa.text(
            "SELECT 1 FROM information_schema.columns "
            f"WHERE table_name='{table}' AND column_name='{col}'"
        ))
        return r.fetchone() is not None

    async with AsyncSessionLocal() as db:
        # ── users.email → nullable ──────────────────────────────────────────
        if is_pg:
            try:
                r = await db.execute(sa.text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name='users' AND column_name='email'"
                ))
                row = r.fetchone()
                if row and row[0] == "NO":
                    await _run(db, "ALTER TABLE users ALTER COLUMN email DROP NOT NULL",
                               "users.email → nullable")
            except Exception as e:
                print(f"⚠️ schema fix users.email: {e}", flush=True)

        # ── users.phone → unique index ──────────────────────────────────────
        if is_pg:
            try:
                r = await db.execute(sa.text(
                    "SELECT 1 FROM pg_indexes WHERE indexname='ix_users_phone'"
                ))
                if not r.fetchone():
                    await db.execute(sa.text(
                        "UPDATE users SET phone = '+700000' || LPAD(id::text, 7, '0') "
                        "WHERE phone IS NULL OR phone = ''"
                    ))
                    await db.execute(sa.text("ALTER TABLE users ALTER COLUMN phone SET NOT NULL"))
                    await db.execute(sa.text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_phone ON users(phone)"
                    ))
                    await db.commit()
                    print("✅ schema fix: users.phone unique index added", flush=True)
            except Exception as e:
                await db.rollback()
                print(f"⚠️ schema fix users.phone index: {e}", flush=True)

        # ── users.phone → nullable (Telegram-самозапись без телефона) ───────
        if is_pg:
            try:
                r = await db.execute(sa.text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name='users' AND column_name='phone'"
                ))
                row = r.fetchone()
                if row and row[0] == "NO":
                    await _run(db, "ALTER TABLE users ALTER COLUMN phone DROP NOT NULL",
                               "users.phone → nullable")
            except Exception as e:
                print(f"⚠️ schema fix users.phone nullable: {e}", flush=True)

        # ── users.hashed_password → nullable (Telegram-вход без пароля) ──────
        if is_pg:
            try:
                r = await db.execute(sa.text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name='users' AND column_name='hashed_password'"
                ))
                row = r.fetchone()
                if row and row[0] == "NO":
                    await _run(db, "ALTER TABLE users ALTER COLUMN hashed_password DROP NOT NULL",
                               "users.hashed_password → nullable")
            except Exception as e:
                print(f"⚠️ schema fix users.hashed_password nullable: {e}", flush=True)

        # ── users.telegram_id → unique index (первичный ид. для Telegram-входа) ─
        if is_pg:
            try:
                r = await db.execute(sa.text(
                    "SELECT 1 FROM pg_indexes WHERE indexname='ix_users_telegram_id'"
                ))
                if not r.fetchone():
                    # Обнуляем пустые строки, чтобы они не конфликтовали в unique
                    await db.execute(sa.text(
                        "UPDATE users SET telegram_id = NULL WHERE telegram_id = ''"
                    ))
                    await db.execute(sa.text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_telegram_id ON users(telegram_id)"
                    ))
                    await db.commit()
                    print("✅ schema fix: users.telegram_id unique index added", flush=True)
            except Exception as e:
                await db.rollback()
                print(f"⚠️ schema fix users.telegram_id index: {e}", flush=True)

        # ── otp_codes.phone ─────────────────────────────────────────────────
        try:
            if not await _check_col(db, "otp_codes", "phone"):
                await db.execute(sa.text("ALTER TABLE otp_codes ADD COLUMN phone VARCHAR(30)"))
                await db.execute(sa.text(
                    "UPDATE otp_codes SET phone = COALESCE(email, 'unknown') WHERE phone IS NULL"
                ))
                if is_pg:
                    await db.execute(sa.text("ALTER TABLE otp_codes ALTER COLUMN phone SET NOT NULL"))
                await db.commit()
                print("✅ schema fix: otp_codes.phone added", flush=True)
        except Exception as e:
            await db.rollback()
            print(f"⚠️ schema fix otp_codes.phone: {e}", flush=True)

        # ── otp_codes.email → nullable ──────────────────────────────────────
        if is_pg:
            try:
                r = await db.execute(sa.text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name='otp_codes' AND column_name='email'"
                ))
                row = r.fetchone()
                if row and row[0] == "NO":
                    await _run(db, "ALTER TABLE otp_codes ALTER COLUMN email DROP NOT NULL",
                               "otp_codes.email → nullable")
            except Exception as e:
                print(f"⚠️ schema fix otp_codes.email: {e}", flush=True)

        # ── users.company_name ──────────────────────────────────────────────
        try:
            if not await _check_col(db, "users", "company_name"):
                await _run(db, "ALTER TABLE users ADD COLUMN company_name VARCHAR(150)",
                           "users.company_name added")
        except Exception as e:
            print(f"⚠️ schema fix users.company_name: {e}", flush=True)

        # ── users.last_subscription_reminder ────────────────────────────────
        try:
            if not await _check_col(db, "users", "last_subscription_reminder"):
                await _run(
                    db,
                    "ALTER TABLE users ADD COLUMN last_subscription_reminder TIMESTAMP",
                    "users.last_subscription_reminder added",
                )
        except Exception as e:
            print(f"⚠️ schema fix users.last_subscription_reminder: {e}", flush=True)

        # ── users: реферальная программа ────────────────────────────────────
        try:
            if not await _check_col(db, "users", "referral_code"):
                await _run(db, "ALTER TABLE users ADD COLUMN referral_code VARCHAR(12)",
                           "users.referral_code added")
                if is_pg:
                    await _run(
                        db,
                        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_referral_code "
                        "ON users(referral_code) WHERE referral_code IS NOT NULL",
                        "users.referral_code unique index",
                    )
            if not await _check_col(db, "users", "referred_by"):
                await _run(db, "ALTER TABLE users ADD COLUMN referred_by INTEGER",
                           "users.referred_by added")
        except Exception as e:
            print(f"⚠️ schema fix users.referral: {e}", flush=True)

        # ── locations: все новые колонки через IF NOT EXISTS ────────────────
        _loc_cols = [
            ("ignore_background_media", "BOOLEAN DEFAULT TRUE"),
            ("business_description",    "TEXT"),
            ("greeting_script",         "TEXT"),
            ("upsell_script",           "TEXT"),
            ("track_upsell",            "BOOLEAN DEFAULT TRUE"),
            ("track_greeting",          "BOOLEAN DEFAULT TRUE"),
            ("track_goodbye",           "BOOLEAN DEFAULT TRUE"),
            ("employees",               "JSONB DEFAULT '[]'::jsonb" if is_pg else "JSON"),
            ("custom_phrases",          "JSONB DEFAULT '[]'::jsonb" if is_pg else "JSON"),
            ("menu_json",               "JSONB" if is_pg else "JSON"),
        ]
        for col_name, col_type in _loc_cols:
            await _run(
                db,
                f"ALTER TABLE locations ADD COLUMN IF NOT EXISTS {col_name} {col_type}",
                f"locations.{col_name} ensured",
            )

        # ── reports columns ────────────────────────────────────────────────
        _rep_cols = [
            ("employee_name", "VARCHAR(100)"),
            ("energy_level",  "INTEGER"),
            ("score",         "INTEGER"),
            ("s3_key",        "TEXT"),
        ]
        for _col, _typ in _rep_cols:
            await _run(
                db,
                f"ALTER TABLE reports ADD COLUMN IF NOT EXISTS {_col} {_typ}",
                f"reports.{_col} ensured",
            )

        # ── otp_codes.code → VARCHAR(64) ────────────────────────────────────
        if is_pg:
            try:
                r = await db.execute(sa.text(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_name='otp_codes' AND column_name='code'"
                ))
                row = r.fetchone()
                if row and row[0] is not None and row[0] < 64:
                    await _run(db, "ALTER TABLE otp_codes ALTER COLUMN code TYPE VARCHAR(64)",
                               "otp_codes.code → VARCHAR(64)")
            except Exception as e:
                print(f"⚠️ schema fix otp_codes.code: {e}", flush=True)


async def _setup_telegram_webhook():
    """
    Register webhook URL with Telegram so the bot receives /start, callbacks, etc.
    Without this the bot is deaf — users can't link their accounts.
    """
    if not settings.TELEGRAM_BOT_TOKEN:
        log.info("TELEGRAM_BOT_TOKEN не задан — webhook не регистрируется")
        return
    if not settings.APP_URL:
        log.warning("APP_URL не задан — webhook не регистрируется (нужен публичный URL)")
        return

    webhook_url = f"{settings.APP_URL}/telegram/webhook"
    payload = {"url": webhook_url, "drop_pending_updates": True}
    if settings.TELEGRAM_WEBHOOK_SECRET:
        payload["secret_token"] = settings.TELEGRAM_WEBHOOK_SECRET

    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/setWebhook",
            json=payload,
        )
        if r.status_code == 200 and r.json().get("ok"):
            print(f"✅ Telegram webhook → {webhook_url}", flush=True)
        else:
            log.error(f"Telegram setWebhook failed: {r.status_code} {r.text}")


async def _promote_admin():
    """Помечает юзера с телефоном ADMIN_PHONE как is_admin=true."""
    if not settings.ADMIN_PHONE:
        return
    try:
        from backend.database import AsyncSessionLocal
        from backend.models.user import User
        from sqlalchemy import select
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.phone == settings.ADMIN_PHONE))
            user = result.scalar()
            if user and not user.is_admin:
                user.is_admin = True
                await db.commit()
                print(f"✅ Admin promoted: {settings.ADMIN_PHONE}", flush=True)
            elif not user:
                print(f"⚠️ ADMIN_PHONE={settings.ADMIN_PHONE} не найден в users (зарегистрируйтесь сначала)", flush=True)
    except Exception as e:
        print(f"⚠️ _promote_admin: {e}", flush=True)


@app.on_event("startup")
async def startup():
    try:
        await asyncio.wait_for(_run_alembic(), timeout=25)
    except asyncio.TimeoutError:
        log.warning("⚠️ Alembic timeout (25s) — пропускаем, продолжаем старт")
    except Exception as e:
        log.error(f"Alembic startup error (non-fatal): {e}")

    try:
        await asyncio.wait_for(init_db(), timeout=15)
    except asyncio.TimeoutError:
        log.warning("⚠️ init_db timeout (15s) — продолжаем старт")
    except Exception as e:
        log.error(f"init_db error (non-fatal): {e}")

    try:
        await asyncio.wait_for(_fix_schema(), timeout=15)
    except asyncio.TimeoutError:
        log.warning("⚠️ _fix_schema timeout (15s) — продолжаем старт")
    except Exception as e:
        log.error(f"_fix_schema error (non-fatal): {e}")

    try:
        await _promote_admin()
    except Exception as e:
        log.error(f"_promote_admin error (non-fatal): {e}")

    # Telegram webhook auto-registration — без него бот не получает /start
    try:
        await _setup_telegram_webhook()
    except Exception as e:
        log.error(f"telegram webhook setup error (non-fatal): {e}")

    # Фоновые задачи — храним ссылки чтобы GC не убил их
    app.state.background_tasks = []
    for task_fn in (_retry_worker, _retention_worker, _daily_report_worker):
        try:
            app.state.background_tasks.append(asyncio.create_task(task_fn()))
        except Exception as e:
            log.error(f"Task {task_fn.__name__} error: {e}")

    try:
        from backend.services.health_monitor import run_health_monitor
        app.state.background_tasks.append(asyncio.create_task(run_health_monitor()))
    except Exception as e:
        log.error(f"health_monitor error (non-fatal): {e}")

    try:
        from backend.services.subscription_reminder import run_subscription_reminder
        app.state.background_tasks.append(asyncio.create_task(run_subscription_reminder()))
    except Exception as e:
        log.error(f"subscription_reminder error (non-fatal): {e}")

    print("✅ TrustControl API v3.0 запущен!", flush=True)
    print(f"📡 http://localhost:{settings.PORT}", flush=True)
    if settings.DEBUG:
        print(f"📖 Документация: http://localhost:{settings.PORT}/docs", flush=True)


@app.on_event("shutdown")
async def shutdown():
    print("👋 TrustControl API остановлен")


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    """Лайт-пинг (для UptimeRobot, keep-alive). Всегда 200."""
    return {"status": "ok", "version": "3.0.0"}


@app.get("/health/ready")
async def health_ready():
    """
    Глубокая проверка — БД, OpenAI, Telegram бот.
    Возвращает 503 если хоть что-то не в порядке (чтобы UptimeRobot прислал email).
    """
    import sqlalchemy as sa
    from backend.database import AsyncSessionLocal
    from backend.config import settings as _s

    checks: dict = {"db": "?", "openai": "?", "telegram": "?"}
    healthy = True

    # 1. База
    try:
        async with AsyncSessionLocal() as db:
            await asyncio.wait_for(db.execute(sa.text("SELECT 1")), timeout=5)
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"fail: {str(e)[:80]}"
        healthy = False

    # 2. OpenAI ключ присутствует (полный запрос — дорого, не делаем)
    checks["openai"] = "ok" if _s.OPENAI_API_KEY else "missing OPENAI_API_KEY"
    if not _s.OPENAI_API_KEY:
        healthy = False

    # 3. Telegram бот доступен
    try:
        from backend.services.notifier import get_bot
        bot = get_bot()
        me  = await asyncio.wait_for(bot.get_me(), timeout=5)
        checks["telegram"] = f"ok (@{me.username})"
    except Exception as e:
        checks["telegram"] = f"fail: {str(e)[:80]}"
        healthy = False

    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )


if __name__ == "__main__":
    from datetime import timedelta  # нужен в _retry_worker closure
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level="info",
    )
