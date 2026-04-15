# ════════════════════════════════════════════════════════════
#  API: Статистика
#  SECURITY FIXES:
#  - Обязательная авторизация
#  - Проверка что location_id принадлежит текущему юзеру
# ════════════════════════════════════════════════════════════

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from datetime import datetime, timedelta, date

from backend.database import get_db
from backend.models.report import Report
from backend.models.alert import Alert
from backend.models.location import Location
from backend.models.user import User
from backend.api.auth import get_current_user

router = APIRouter()


async def _get_user_location_ids(user_id: int, db: AsyncSession) -> list[int]:
    """Возвращает список ID точек этого пользователя."""
    result = await db.execute(
        select(Location.id).where(
            Location.owner_id == user_id,
            Location.is_active == True
        )
    )
    return [r[0] for r in result.all()]


@router.get("/dashboard")
async def dashboard(
    location_id: int = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),  # ── SECURITY: обязательная авторизация
):
    """Статистика для дашборда. Только по точкам текущего пользователя."""

    user_location_ids = await _get_user_location_ids(user.id, db)

    if not user_location_ids:
        return {"today": {"total": 0, "score": 0, "greetings_pct": 0, "bonus_pct": 0,
                          "bad_count": 0, "fraud_count": 0, "positive_tone": 0, "negative_tone": 0},
                "week": [], "alerts_today": 0}

    # ── SECURITY: проверяем что запрошенная точка принадлежит юзеру
    if location_id:
        if location_id not in user_location_ids:
            raise HTTPException(status_code=403, detail="Нет доступа к этой точке")
        filter_ids = [location_id]
    else:
        filter_ids = user_location_ids

    today = date.today()

    # ── Отчёты за сегодня ────────────────────────────────────
    today_result = await db.execute(
        select(Report).where(
            Report.location_id.in_(filter_ids),
            func.date(Report.timestamp) == today
        )
    )
    today_reports = today_result.scalars().all()
    total = len(today_reports)

    if total == 0:
        return {"today": {"total": 0, "score": 0, "greetings_pct": 0, "bonus_pct": 0,
                          "bad_count": 0, "fraud_count": 0, "positive_tone": 0, "negative_tone": 0},
                "week": [], "alerts_today": 0}

    def pct(lst, flag):
        return round(sum(1 for r in lst if getattr(r, flag)) / len(lst) * 100) if lst else 0

    # ── Оценка за день ───────────────────────────────────────
    score = (
        pct(today_reports, "has_greeting") * 0.25 +
        pct(today_reports, "has_thanks")   * 0.20 +
        pct(today_reports, "has_goodbye")  * 0.15 +
        pct(today_reports, "has_bonus")    * 0.25 +
        sum(1 for r in today_reports if r.tone == "positive") / total * 100 * 0.15 -
        sum(1 for r in today_reports if r.has_bad or r.has_fraud) * 10
    )

    # ── Тревоги за сегодня ───────────────────────────────────
    alerts_result = await db.execute(
        select(func.count(Alert.id)).where(
            Alert.location_id.in_(filter_ids),
            func.date(Alert.timestamp) == today,
            Alert.is_resolved == False,
        )
    )
    alerts_count = alerts_result.scalar() or 0

    # ── График за 7 дней ─────────────────────────────────────
    week_data = []
    for i in range(7):
        d = today - timedelta(days=i)
        day_result = await db.execute(
            select(Report).where(
                Report.location_id.in_(filter_ids),
                func.date(Report.timestamp) == d
            )
        )
        day_reports = day_result.scalars().all()
        week_data.append({
            "date":          str(d),
            "total":         len(day_reports),
            "greetings_pct": pct(day_reports, "has_greeting"),
            "bonus_pct":     pct(day_reports, "has_bonus"),
            "fraud_count":   sum(1 for r in day_reports if r.has_fraud),
        })

    return {
        "today": {
            "total":         total,
            "greetings_pct": pct(today_reports, "has_greeting"),
            "thanks_pct":    pct(today_reports, "has_thanks"),
            "goodbye_pct":   pct(today_reports, "has_goodbye"),
            "bonus_pct":     pct(today_reports, "has_bonus"),
            "bad_count":     sum(1 for r in today_reports if r.has_bad),
            "fraud_count":   sum(1 for r in today_reports if r.has_fraud),
            "positive_tone": sum(1 for r in today_reports if r.tone == "positive"),
            "negative_tone": sum(1 for r in today_reports if r.tone == "negative"),
            "score":         max(0, min(100, round(score))),
        },
        "week":         week_data,
        "alerts_today": alerts_count,
    }
