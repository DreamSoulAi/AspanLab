# ════════════════════════════════════════════════════════════
#  API: Тревоги
#  SECURITY FIXES:
#  - Фильтрация только по точкам текущего пользователя
#  - Проверка владельца при resolve
#  - Лимит days максимум 90
# ════════════════════════════════════════════════════════════

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timedelta
from pydantic import BaseModel
from typing import Optional

from backend.database import get_db
from backend.models.alert import Alert
from backend.models.location import Location
from backend.api.auth import get_current_user
from backend.models.user import User

router = APIRouter()


class ResolveRequest(BaseModel):
    notes: Optional[str] = None


async def _get_user_location_ids(user_id: int, db: AsyncSession) -> list[int]:
    result = await db.execute(
        select(Location.id).where(Location.owner_id == user_id)
    )
    return [r[0] for r in result.all()]


@router.get("/")
async def list_alerts(
    location_id: int = None,
    alert_type: str = None,
    days: int = 7,
    unresolved_only: bool = True,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Тревоги только по точкам текущего пользователя."""

    # ── SECURITY: лимит периода ──────────────────────────────
    days = min(days, 90)
    since = datetime.utcnow() - timedelta(days=days)

    # ── SECURITY: только точки этого юзера ──────────────────
    user_location_ids = await _get_user_location_ids(user.id, db)
    if not user_location_ids:
        return []

    # ── SECURITY: проверяем запрошенную точку ────────────────
    if location_id and location_id not in user_location_ids:
        raise HTTPException(status_code=403, detail="Нет доступа к этой точке")

    filter_ids = [location_id] if location_id else user_location_ids

    query = (
        select(Alert)
        .where(
            Alert.location_id.in_(filter_ids),
            Alert.timestamp >= since,
        )
        .order_by(Alert.timestamp.desc())
        .limit(200)
    )

    if alert_type:
        # Белый список допустимых типов
        allowed_types = {"fraud", "bad_language", "negative_tone", "no_greeting", "no_goodbye"}
        if alert_type not in allowed_types:
            raise HTTPException(status_code=400, detail="Недопустимый тип тревоги")
        query = query.where(Alert.alert_type == alert_type)

    if unresolved_only:
        query = query.where(Alert.is_resolved == False)

    result = await db.execute(query)
    alerts = result.scalars().all()

    return [
        {
            "id":             a.id,
            "location_id":    a.location_id,
            "timestamp":      a.timestamp.isoformat(),
            "alert_type":     a.alert_type,
            "severity":       a.severity,
            "transcript":     (a.transcript or "")[:400],
            "trigger_phrase": a.trigger_phrase,
            "is_resolved":    a.is_resolved,
            "resolved_at":    a.resolved_at.isoformat() if a.resolved_at else None,
            "manager_notes":  a.manager_notes,
        }
        for a in alerts
    ]


@router.patch("/{alert_id}/resolve")
async def resolve_alert(
    alert_id: int,
    data: ResolveRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Пометить тревогу как решённую. Только владелец точки."""
    alert = await db.get(Alert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Тревога не найдена")

    # ── SECURITY: проверяем что тревога принадлежит точке юзера
    user_location_ids = await _get_user_location_ids(user.id, db)
    if alert.location_id not in user_location_ids:
        raise HTTPException(status_code=403, detail="Нет доступа к этой тревоге")

    alert.is_resolved   = True
    alert.resolved_at   = datetime.utcnow()
    alert.resolved_by   = user.name
    alert.manager_notes = (data.notes or "")[:500]  # ограничиваем длину заметки

    return {"message": "Тревога отмечена как решённая"}
