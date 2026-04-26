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


class LocationUpdate(BaseModel):
    name:                      Optional[str]  = None
    business_type:             Optional[str]  = None
    address:                   Optional[str]  = None
    city:                      Optional[str]  = None
    telegram_chat:             Optional[str]  = None
    language:                  Optional[str]  = None
    vad_level:                 Optional[int]  = None
    ignore_internal_profanity: Optional[bool] = None


class AntifraudSettings(BaseModel):
    allowed_phones:   Optional[list[str]] = None
    required_upsells: Optional[list[str]] = None


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
            "address":       loc.address,
            "language":      loc.language,
            "is_active":     loc.is_active,
            "api_key":       loc.api_key,
            "telegram_chat": loc.telegram_chat,
            "last_seen":     loc.last_seen.isoformat() if loc.last_seen else None,
            "allowed_phones":            loc.allowed_phones or [],
            "required_upsells":          loc.required_upsells or [],
            "ignore_internal_profanity": bool(loc.ignore_internal_profanity),
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


@router.patch("/{location_id}")
async def update_location(
    location_id: int,
    data: LocationUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    loc = await db.get(Location, location_id)
    if not loc or loc.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Точка не найдена")

    if data.name                      is not None: loc.name                      = data.name
    if data.business_type             is not None: loc.business_type             = data.business_type
    if data.address                   is not None: loc.address                   = data.address
    if data.city                      is not None: loc.city                      = data.city
    if data.telegram_chat             is not None: loc.telegram_chat             = data.telegram_chat
    if data.language                  is not None: loc.language                  = data.language
    if data.vad_level                 is not None: loc.vad_level                 = data.vad_level
    if data.ignore_internal_profanity is not None: loc.ignore_internal_profanity = data.ignore_internal_profanity

    return {"message": "Точка обновлена", "id": loc.id}


@router.put("/{location_id}/antifraud")
async def update_antifraud(
    location_id: int,
    data: AntifraudSettings,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Обновляет антифрод-настройки точки:
      allowed_phones   — белый список Каспи-номеров владельца
      required_upsells — обязательные фразы допродажи для UPSELL_GAP детектора
    """
    loc = await db.get(Location, location_id)
    if not loc or loc.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Точка не найдена")

    if data.allowed_phones is not None:
        loc.allowed_phones   = data.allowed_phones
    if data.required_upsells is not None:
        loc.required_upsells = data.required_upsells

    await db.commit()
    return {
        "message":         "Антифрод-настройки обновлены",
        "allowed_phones":   loc.allowed_phones,
        "required_upsells": loc.required_upsells,
    }


@router.post("/{location_id}/test-telegram")
async def test_telegram(
    location_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Отправляет тестовое сообщение в Telegram группу точки."""
    loc = await db.get(Location, location_id)
    if not loc or loc.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Точка не найдена")

    chat_id = loc.telegram_chat
    if not chat_id:
        raise HTTPException(status_code=400, detail="Telegram Chat ID не задан в настройках точки")

    from backend.services import notifier
    from datetime import datetime
    try:
        await notifier._send(
            chat_id,
            f"✅ *TrustControl подключён!*\n\n"
            f"🏪 Точка: *{loc.name}*\n"
            f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
            f"Уведомления о нарушениях и итоги смен будут приходить сюда.",
        )
        return {"status": "ok", "message": "Тестовое сообщение отправлено"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка отправки: {e}")

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
