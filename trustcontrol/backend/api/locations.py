# ════════════════════════════════════════════════════════════
#  API: Торговые точки
# ════════════════════════════════════════════════════════════

import secrets
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field, field_validator

from backend.database import get_db
from backend.models.location import Location
from backend.api.auth import get_current_user
from backend.models.user import User

router = APIRouter()

VALID_BUSINESS_TYPES = {"coffee", "gas", "fastfood", "cafe", "beauty", "shop", "fitness", "hotel"}
VALID_LANGUAGES      = {"ru", "kk", "mixed"}


class LocationCreate(BaseModel):
    name:          str  = Field(..., min_length=1, max_length=150)
    business_type: str  = "coffee"
    address:       Optional[str] = Field(None, max_length=255)
    city:          str  = Field("Алматы", max_length=100)
    telegram_chat: Optional[str] = Field(None, max_length=50)
    language:      str  = "ru"
    vad_level:     int  = Field(2, ge=0, le=3)

    business_description: Optional[str] = None
    greeting_script:      Optional[str] = None
    upsell_script:        Optional[str] = None
    track_upsell:         bool = True
    track_greeting:       bool = True
    track_goodbye:        bool = True

    @field_validator("business_type")
    @classmethod
    def validate_business_type(cls, v):
        if v not in VALID_BUSINESS_TYPES:
            raise ValueError(f"business_type должен быть одним из {VALID_BUSINESS_TYPES}")
        return v

    @field_validator("language")
    @classmethod
    def validate_language(cls, v):
        if v not in VALID_LANGUAGES:
            raise ValueError(f"language должен быть одним из {VALID_LANGUAGES}")
        return v


class LocationUpdate(BaseModel):
    name:                      Optional[str]  = Field(None, max_length=150)
    business_type:             Optional[str]  = None
    address:                   Optional[str]  = Field(None, max_length=255)
    city:                      Optional[str]  = Field(None, max_length=100)
    telegram_chat:             Optional[str]  = Field(None, max_length=50)
    language:                  Optional[str]  = None
    vad_level:                 Optional[int]  = Field(None, ge=0, le=3)
    ignore_internal_profanity: Optional[bool] = None
    ignore_background_media:   Optional[bool] = None
    notify_ok_conversations:   Optional[bool] = None

    business_description: Optional[str] = None
    greeting_script:      Optional[str] = None
    upsell_script:        Optional[str] = None
    track_upsell:         Optional[bool] = None
    track_greeting:       Optional[bool] = None
    track_goodbye:        Optional[bool] = None

    @field_validator("business_type")
    @classmethod
    def validate_business_type(cls, v):
        if v is not None and v not in VALID_BUSINESS_TYPES:
            raise ValueError(f"business_type должен быть одним из {VALID_BUSINESS_TYPES}")
        return v

    @field_validator("language")
    @classmethod
    def validate_language(cls, v):
        if v is not None and v not in VALID_LANGUAGES:
            raise ValueError(f"language должен быть одним из {VALID_LANGUAGES}")
        return v


class AntifraudSettings(BaseModel):
    allowed_phones:   Optional[list[str]] = None
    required_upsells: Optional[list[str]] = None

    @field_validator("allowed_phones")
    @classmethod
    def validate_phones(cls, v):
        if v is not None:
            if len(v) > 50:
                raise ValueError("Максимум 50 номеров в белом списке")
            for p in v:
                if len(p) > 20:
                    raise ValueError(f"Номер слишком длинный: {p[:20]}")
        return v

    @field_validator("required_upsells")
    @classmethod
    def validate_upsells(cls, v):
        if v is not None:
            if len(v) > 30:
                raise ValueError("Максимум 30 фраз в списке допродаж")
            for phrase in v:
                if len(phrase) > 100:
                    raise ValueError("Фраза допродажи слишком длинная (макс. 100 символов)")
        return v


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
            "ignore_background_media":   bool(getattr(loc, "ignore_background_media", True)),
            "notify_ok_conversations":   bool(getattr(loc, "notify_ok_conversations", False)),
            "business_description": loc.business_description,
            "greeting_script":      loc.greeting_script,
            "upsell_script":        loc.upsell_script,
            "track_upsell":         bool(getattr(loc, "track_upsell", True)),
            "track_greeting":       bool(getattr(loc, "track_greeting", True)),
            "track_goodbye":        bool(getattr(loc, "track_goodbye", True)),
        }
        for loc in locations
    ]


@router.get("/{location_id}")
async def get_location(
    location_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    loc = await db.get(Location, location_id)
    if not loc or loc.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Точка не найдена")
    return {
        "id":            loc.id,
        "name":          loc.name,
        "business_type": loc.business_type,
        "city":          loc.city,
        "address":       loc.address,
        "language":      loc.language,
        "is_active":     loc.is_active,
        "api_key":       loc.api_key,
        "telegram_chat": loc.telegram_chat,
        "allowed_phones":            loc.allowed_phones or [],
        "ignore_internal_profanity": bool(loc.ignore_internal_profanity),
        "ignore_background_media":   bool(getattr(loc, "ignore_background_media", True)),
        "notify_ok_conversations":   bool(getattr(loc, "notify_ok_conversations", False)),
        "business_description": loc.business_description,
        "greeting_script":      loc.greeting_script,
        "upsell_script":        loc.upsell_script,
        "track_upsell":         bool(getattr(loc, "track_upsell", True)),
        "track_greeting":       bool(getattr(loc, "track_greeting", True)),
        "track_goodbye":        bool(getattr(loc, "track_goodbye", True)),
    }


@router.post("/")
async def create_location(
    data: LocationCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from backend.services.subscription import get_status as _sub_status
    if _sub_status(user) == "blocked":
        raise HTTPException(
            status_code=402,
            detail="Подписка истекла. Оплатите для добавления новых точек.",
        )

    result = await db.execute(
        select(Location).where(Location.owner_id == user.id, Location.is_active == True)
    )
    existing = result.scalars().all()

    limits = {"trial": 1, "start": 1, "business": 3, "potok": 5, "network": 999}
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
        api_key=secrets.token_hex(32),
        business_description=data.business_description,
        greeting_script=data.greeting_script,
        upsell_script=data.upsell_script,
        track_upsell=data.track_upsell,
        track_greeting=data.track_greeting,
        track_goodbye=data.track_goodbye,
    )
    db.add(loc)
    await db.commit()
    await db.refresh(loc)

    return {
        "id":      loc.id,
        "name":    loc.name,
        "api_key": loc.api_key,
        "message": "Точка создана. Используйте api_key в config.ini на кассе.",
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
    if data.ignore_background_media   is not None: loc.ignore_background_media   = data.ignore_background_media
    if data.notify_ok_conversations   is not None: loc.notify_ok_conversations   = data.notify_ok_conversations
    if data.business_description      is not None: loc.business_description      = data.business_description
    if data.greeting_script           is not None: loc.greeting_script           = data.greeting_script
    if data.upsell_script             is not None: loc.upsell_script             = data.upsell_script
    if data.track_upsell              is not None: loc.track_upsell              = data.track_upsell
    if data.track_greeting            is not None: loc.track_greeting            = data.track_greeting
    if data.track_goodbye             is not None: loc.track_goodbye             = data.track_goodbye

    await db.commit()
    return {"message": "Точка обновлена", "id": loc.id}


@router.put("/{location_id}/antifraud")
async def update_antifraud(
    location_id: int,
    data: AntifraudSettings,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
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
    loc = await db.get(Location, location_id)
    if not loc or loc.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Точка не найдена")

    chat_id = loc.telegram_chat
    if not chat_id:
        raise HTTPException(status_code=400, detail="Telegram Chat ID не задан в настройках точки")

    from backend.services import notifier
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


@router.post("/{location_id}/tg-link")
async def location_tg_link(
    location_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Генерирует одноразовый токен для привязки Telegram к точке.
    Если TELEGRAM_BOT_USERNAME не задан — получает его из Telegram API.
    """
    loc = await db.get(Location, location_id)
    if not loc or loc.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Точка не найдена")

    from backend.api.telegram_webhook import generate_link_token
    from backend.config import settings

    token    = generate_link_token({"type": "location", "location_id": location_id, "user_id": user.id})
    bot_name = settings.TELEGRAM_BOT_USERNAME

    if not bot_name:
        try:
            from backend.services.notifier import get_bot
            me       = await get_bot().get_me()
            bot_name = me.username
        except Exception:
            pass

    url = f"https://t.me/{bot_name}?start={token}" if bot_name else None
    return {"token": token, "url": url, "bot_username": bot_name}


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
    await db.commit()
    return {"message": "Точка отключена"}
