# ════════════════════════════════════════════════════════════
#  API: Health Check / Ping воркера
#
#  POST /api/v1/health/ping   — воркер шлёт каждые 5 мин
#  GET  /api/v1/health/status — дашборд запрашивает статусы
# ════════════════════════════════════════════════════════════

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.database import get_db
from backend.models.location import Location
from backend.models.user import User
from backend.api.auth import get_current_user

log = logging.getLogger("health")
router = APIRouter()

OFFLINE_THRESHOLD_MINUTES = 10


# ── POST /ping ────────────────────────────────────────────────────────────────

@router.post("/ping")
async def worker_ping(
    api_key:   Optional[str] = Header(None, alias="X-API-Key"),
    x_api_key: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Воркер на кассе вызывает этот эндпоинт каждые 5 минут.
    Обновляет last_ping_at у точки.
    """
    key = (api_key or x_api_key or "").strip()
    if not key:
        raise HTTPException(status_code=401, detail="Нужен X-API-Key")

    result = await db.execute(
        select(Location).where(Location.api_key == key, Location.is_active == True)
    )
    loc = result.scalar()
    if not loc:
        raise HTTPException(status_code=401, detail="Неверный API ключ")

    now = datetime.utcnow()
    loc.last_ping_at = now
    # Если раньше был офлайн — сбрасываем флаг чтобы следующий выход вызвал алерт
    if loc.offline_alerted_at:
        loc.offline_alerted_at = None
    await db.commit()

    return {"status": "ok", "timestamp": now.isoformat()}


# ── GET /status ───────────────────────────────────────────────────────────────

@router.get("/status")
async def get_status(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Возвращает статус всех точек пользователя:
      online   — ping был < 10 мин назад
      offline  — ping > 10 мин назад
      unknown  — ping ни разу не поступал (воркер ещё не запущен)
    """
    result = await db.execute(
        select(Location).where(Location.owner_id == user.id, Location.is_active == True)
    )
    locations = result.scalars().all()

    now = datetime.utcnow()
    statuses = []

    for loc in locations:
        if not loc.last_ping_at:
            status = "unknown"
            minutes_ago = None
        else:
            delta = (now - loc.last_ping_at).total_seconds() / 60
            status = "online" if delta < OFFLINE_THRESHOLD_MINUTES else "offline"
            minutes_ago = round(delta)

        statuses.append({
            "location_id":   loc.id,
            "location_name": loc.name,
            "status":        status,
            "last_ping_at":  loc.last_ping_at.isoformat() if loc.last_ping_at else None,
            "minutes_ago":   minutes_ago,
        })

    return statuses
