# ╔══════════════════════════════════════════════════════════╗
# ║              TrustControl — Главный файл                  ║
# ║         Запускает API сервер для всех точек               ║
# ╚══════════════════════════════════════════════════════════╝
#
# ЗАПУСК:
#   pip install -r requirements.txt
#   python main.py
#
# API:           http://localhost:8000
# Документация:  http://localhost:8000/docs

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
from backend.database       import init_db
from backend.config         import settings

# ── Создаём приложение ───────────────────────────────────────
app = FastAPI(
    title="TrustControl API",
    description="ИИ-мониторинг качества обслуживания",
    version="1.0.0",
    # В продакшне скрываем документацию
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url=None,
)

# ── CORS ─────────────────────────────────────────────────────
# В продакшне указываем конкретный домен из .env
ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:8000"
).split(",")

# В DEBUG режиме разрешаем все origins для разработки
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
app.include_router(auth_router,      prefix="/api/auth",      tags=["Auth"])
app.include_router(locations_router, prefix="/api/locations", tags=["Locations"])
app.include_router(reports_router,   prefix="/api/reports",   tags=["Reports"])
app.include_router(alerts_router,    prefix="/api/alerts",    tags=["Alerts"])
app.include_router(stats_router,     prefix="/api/stats",     tags=["Stats"])

# ── Фронтенд (только если папка существует) ──────────────────
DASHBOARD_DIR = Path(__file__).parent / "frontend" / "dashboard"
if DASHBOARD_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(DASHBOARD_DIR), html=True), name="dashboard")

    @app.get("/")
    async def root():
        return FileResponse(str(DASHBOARD_DIR / "index.html"))

# ── События запуска ──────────────────────────────────────────
@app.on_event("startup")
async def startup():
    await init_db()
    print("✅ TrustControl API запущен!")
    print(f"📡 http://localhost:{settings.PORT}")
    if settings.DEBUG:
        print(f"📖 Документация: http://localhost:{settings.PORT}/docs")

@app.on_event("shutdown")
async def shutdown():
    print("👋 TrustControl API остановлен")

# ── Health check для мониторинга ─────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level="info",
    )
