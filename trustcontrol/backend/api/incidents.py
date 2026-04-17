# ════════════════════════════════════════════════════════════
#  API: Инциденты (лента фрода и нарушений)
#
#  GET  /api/v1/incidents        — список инцидентов
#  GET  /api/v1/incidents/{id}   — детали
#  POST /api/v1/incidents/{id}/resolve  — закрыть вручную
# ════════════════════════════════════════════════════════════

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.database import get_db
from backend.models.incident import Incident
from backend.models.location import Location
from backend.models.user import User
from backend.api.auth import get_current_user

log    = logging.getLogger("incidents")
router = APIRouter()


class ResolveRequest(BaseModel):
    status: str = "resolved"   # resolved | false_positive


@router.get("/")
async def list_incidents(
    location_id:   Optional[int]  = None,
    incident_type: Optional[str]  = None,
    status:        Optional[str]  = None,   # open | resolved | false_positive
    days:          int            = 7,
    limit:         int            = 50,
    db:            AsyncSession   = Depends(get_db),
    user:          User           = Depends(get_current_user),
):
    """
    Возвращает ленту инцидентов за последние N дней.
    Используется фронтендом для ленты фрода.
    """
    limit = min(limit, 200)

    locs_r = await db.execute(
        select(Location.id, Location.name).where(
            Location.owner_id == user.id,
            Location.is_active == True,
        )
    )
    rows     = locs_r.all()
    loc_map  = {r[0]: r[1] for r in rows}
    loc_ids  = list(loc_map.keys())
    if not loc_ids:
        return []

    since = datetime.utcnow() - timedelta(days=days)
    q = (
        select(Incident)
        .where(
            Incident.location_id.in_(loc_ids),
            Incident.created_at >= since,
        )
        .order_by(Incident.created_at.desc())
        .limit(limit)
    )
    if location_id:
        if location_id not in loc_ids:
            raise HTTPException(status_code=403, detail="Нет доступа")
        q = q.where(Incident.location_id == location_id)
    if incident_type:
        q = q.where(Incident.incident_type == incident_type)
    if status:
        q = q.where(Incident.status == status)

    result = await db.execute(q)
    items  = result.scalars().all()

    now = datetime.utcnow()
    return [
        {
            "id":            i.id,
            "location_id":   i.location_id,
            "location_name": loc_map.get(i.location_id, "—"),
            "incident_type": i.incident_type,
            "severity":      i.severity,
            "description":   i.description,
            "detected_phone": i.detected_phone,
            "upsell_phrase": i.upsell_phrase,
            "proof_s3_url":  i.proof_s3_url,
            "status":        i.status,
            "created_at":    i.created_at.isoformat(),
            "minutes_ago":   round((now - i.created_at).total_seconds() / 60),
        }
        for i in items
    ]


@router.post("/{incident_id}/resolve")
async def resolve_incident(
    incident_id: int,
    body: ResolveRequest,
    db:   AsyncSession = Depends(get_db),
    user: User         = Depends(get_current_user),
):
    """Закрывает инцидент вручную (resolved или false_positive)."""
    incident = await db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Инцидент не найден")

    # Проверяем что точка принадлежит пользователю
    loc = await db.get(Location, incident.location_id)
    if not loc or loc.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Нет доступа")

    valid = {"resolved", "false_positive"}
    if body.status not in valid:
        raise HTTPException(status_code=400, detail=f"status должен быть одним из {valid}")

    incident.status      = body.status
    incident.resolved_at = datetime.utcnow()

    # Если false_positive + KASPI_FRAUD → авто-добавляем в белый список
    if body.status == "false_positive" and incident.incident_type == "KASPI_FRAUD":
        if incident.detected_phone and loc:
            phones = list(loc.allowed_phones or [])
            if incident.detected_phone not in phones:
                phones.append(incident.detected_phone)
                loc.allowed_phones = phones
                log.info(f"[loc={loc.id}] {incident.detected_phone} добавлен в белый список")

    await db.commit()
    return {"ok": True, "status": incident.status}
