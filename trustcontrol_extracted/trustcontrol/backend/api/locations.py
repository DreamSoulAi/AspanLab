# ════════════════════════════════════════════════════════════
#  API: Торговые точки
# ════════════════════════════════════════════════════════════

import secrets
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional

from backend.database import get_db
from backend.models.location import Location
from backend.api.auth import get_current_user
from backend.models.user import User

router = APIRouter()


class LocationCreate(BaseModel):
    name:          str
    business_type: str = "coffee"
    address:       Optional[str] = None
    city:          str = "Алматы"
    telegram_chat: Optional[str] = None
    language:      str = "ru"
    vad_level:     int = 2


@router.get("/")
async def list_locations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Location).where(Location.owner_id == user.id)
    )
    locations = result.scalars().all()
    return [
        {
            "id":            loc.id,
            "name":          loc.name,
            "business_type": loc.business_type,
            "city":          loc.city,
            "is_active":     loc.is_active,
            "api_key":       loc.api_key,
            "last_seen":     loc.last_seen.isoformat() if loc.last_seen else None,
        }
        for loc in locations
    ]


@router.post("/")
async def create_location(
    data: LocationCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Проверяем лимит по тарифу
    result = await db.execute(
        select(Location).where(Location.owner_id == user.id, Location.is_active == True)
    )
    existing = result.scalars().all()

    limits = {"trial": 1, "start": 1, "business": 5, "network": 999}
    limit = limits.get(user.plan, 1)

    if len(existing) >= limit:
        raise HTTPException(
            status_code=403,
            detail=f"Ваш тариф «{user.plan}» позволяет максимум {limit} точек. Обновите тариф."
        )

    loc = Location(
        owner_id=user.id,
        name=data.name,
        business_type=data.business_type,
        address=data.address,
        city=data.city,
        telegram_chat=data.telegram_chat,
        language=data.language,
        vad_level=data.vad_level,
        api_key=secrets.token_hex(32),  # уникальный ключ для скрипта
    )
    db.add(loc)
    await db.flush()

    return {
        "id":      loc.id,
        "name":    loc.name,
        "api_key": loc.api_key,
        "message": "Точка создана. Используйте api_key в config.py на кассе.",
    }


@router.delete("/{location_id}")
async def delete_location(
    location_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    loc = await db.get(Location, location_id)
    if not loc or loc.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Точка не найдена")
    loc.is_active = False
    return {"message": "Точка отключена"}
