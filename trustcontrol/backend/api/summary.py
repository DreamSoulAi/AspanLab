# ════════════════════════════════════════════════════════════
#  API: Сводка сети одним запросом
#
#  GET /api/v1/summary
#  Отдаёт всё состояние за один запрос:
#    • статус воркеров
#    • счётчики фрода за сегодня
#    • средний % допродаж
#    • открытые инциденты
#    • упущенная выручка (UPSELL_GAP × 500 ₸)
# ════════════════════════════════════════════════════════════

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from backend.database import get_db
from backend.models.location import Location
from backend.models.report import Report
from backend.models.incident import Incident
from backend.models.user import User
from backend.api.auth import get_current_user

log = logging.getLogger("summary")
router = APIRouter()

OFFLINE_MIN       = 10
RECORDING_IDLE_S  = 60
AVG_UPSELL_TENGE  = 500   # грубая оценка упущенного чека


@router.get("/")
async def get_summary(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Единый снапшот состояния всей сети владельца.

    Используется фронтендом для первичной загрузки и авто-обновления.
    """
    now         = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # ── Точки ────────────────────────────────────────────────
    locs_r = await db.execute(
        select(Location).where(Location.owner_id == user.id, Location.is_active == True)
    )
    locations = locs_r.scalars().all()
    loc_ids   = [l.id for l in locations]

    workers = []
    for loc in locations:
        if not loc.last_ping_at:
            status      = "unknown"
            minutes_ago = None
        else:
            delta_min   = (now - loc.last_ping_at).total_seconds() / 60
            status      = "online" if delta_min < OFFLINE_MIN else "offline"
            minutes_ago = round(delta_min)

        recording_active = bool(
            loc.last_seen and (now - loc.last_seen).total_seconds() <= RECORDING_IDLE_S
        )
        workers.append({
            "location_id":      loc.id,
            "name":             loc.name,
            "status":           status,
            "recording_active": recording_active,
            "minutes_ago":      minutes_ago,
        })

    if not loc_ids:
        return {
            "workers":        workers,
            "today":          _empty_today(),
            "open_incidents": 0,
        }

    # ── Отчёты за сегодня ────────────────────────────────────
    reps_r = await db.execute(
        select(Report).where(
            Report.location_id.in_(loc_ids),
            Report.timestamp  >= today_start,
            Report.is_hidden  == False,
        )
    )
    reports = reps_r.scalars().all()
    total   = len(reports)

    fraud_today  = sum(1 for r in reports if r.fraud_status == "critical_fraud_risk")
    upsell_count = sum(1 for r in reports if r.upsell_attempt)
    greet_count  = sum(1 for r in reports if r.has_greeting)
    neg_count    = sum(1 for r in reports if r.tone == "negative")
    sat_scores   = [r.customer_satisfaction for r in reports if r.customer_satisfaction]
    avg_sat      = round(sum(sat_scores) / len(sat_scores), 2) if sat_scores else 0.0
    avg_score    = round(sum(r.gpt_score for r in reports if r.gpt_score) /
                         max(1, sum(1 for r in reports if r.gpt_score)), 1)

    upsell_pct  = round(upsell_count / total * 100, 1) if total else 0.0
    greet_pct   = round(greet_count  / total * 100, 1) if total else 0.0

    # ── Инциденты ─────────────────────────────────────────────
    open_r = await db.execute(
        select(func.count(Incident.id)).where(
            Incident.location_id.in_(loc_ids),
            Incident.status == "open",
        )
    )
    open_count = open_r.scalar() or 0

    gap_r = await db.execute(
        select(func.count(Incident.id)).where(
            Incident.location_id.in_(loc_ids),
            Incident.incident_type == "UPSELL_GAP",
            Incident.created_at    >= today_start,
        )
    )
    upsell_gaps = gap_r.scalar() or 0

    return {
        "workers": workers,
        "today": {
            "total_conversations":  total,
            "fraud_count":          fraud_today,
            "upsell_pct":           upsell_pct,
            "greeting_pct":         greet_pct,
            "avg_satisfaction":     avg_sat,
            "avg_score":            avg_score,
            "negative_count":       neg_count,
            "upsell_gaps":          upsell_gaps,
            "missed_upsell_revenue": upsell_gaps * AVG_UPSELL_TENGE,
        },
        "open_incidents": open_count,
    }


def _empty_today():
    return {
        "total_conversations": 0,
        "fraud_count": 0,
        "upsell_pct": 0.0,
        "greeting_pct": 0.0,
        "avg_satisfaction": 0.0,
        "avg_score": 0.0,
        "negative_count": 0,
        "upsell_gaps": 0,
        "missed_upsell_revenue": 0,
    }
