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

    # Только реальные диалоги с клиентами (не тесты, не фоновые звуки)
    qualified_reports = [
        r for r in today_reports
        if r.conversation_context == 'customer_service'
    ]
    qualified = len(qualified_reports)

    if total == 0:
        return {"today": {"total": 0, "qualified": 0, "score": 0, "greetings_pct": 0, "bonus_pct": 0,
                          "bad_count": 0, "fraud_count": 0, "positive_tone": 0, "negative_tone": 0},
                "week": [], "alerts_today": 0}

    def pct(lst, flag):
        return round(sum(1 for r in lst if getattr(r, flag)) / len(lst) * 100) if lst else 0

    def _report_score(r):
        # Единый источник истины: финальный балл движка.
        # Старые отчёты (до движка) — fallback на gpt_score.
        return r.score if r.score is not None else r.gpt_score

    # ── Оценка за день — среднее по единому движку ───────────
    _day_scores = [s for r in qualified_reports if (s := _report_score(r)) is not None]
    score = sum(_day_scores) / len(_day_scores) if _day_scores else 0

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
        day_qualified = [r for r in day_reports if r.conversation_context == 'customer_service']
        week_data.append({
            "date":          str(d),
            "total":         len(day_reports),
            "qualified":     len(day_qualified),
            "greetings_pct": pct(day_qualified, "has_greeting"),
            "bonus_pct":     pct(day_qualified, "has_bonus"),
            "fraud_count":   sum(1 for r in day_qualified if r.has_fraud),
        })

    return {
        "today": {
            "total":         total,
            "qualified":     qualified,
            "greetings_pct": pct(qualified_reports, "has_greeting"),
            "thanks_pct":    pct(qualified_reports, "has_thanks"),
            "goodbye_pct":   pct(qualified_reports, "has_goodbye"),
            "bonus_pct":     pct(qualified_reports, "has_bonus"),
            "bad_count":     sum(1 for r in qualified_reports if r.has_bad),
            "fraud_count":   sum(1 for r in qualified_reports if r.has_fraud),
            "positive_tone": sum(1 for r in qualified_reports if r.tone == "positive"),
            "negative_tone": sum(1 for r in qualified_reports if r.tone == "negative"),
            "score":         max(0, min(100, round(score))),
        },
        "week":         week_data,
        "alerts_today": alerts_count,
    }


@router.get("/employees")
async def employees_stats(
    location_id: int = None,
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Аналитика по сотрудникам за период (по умолчанию 30 дней).
    Группирует разговоры по employee_name и считает качество каждого.
    """
    days = max(1, min(days, 365))
    user_location_ids = await _get_user_location_ids(user.id, db)
    if not user_location_ids:
        return {"employees": [], "days": days}

    if location_id:
        if location_id not in user_location_ids:
            raise HTTPException(status_code=403, detail="Нет доступа к этой точке")
        filter_ids = [location_id]
    else:
        filter_ids = user_location_ids

    since = datetime.utcnow() - timedelta(days=days)
    result = await db.execute(
        select(Report).where(
            Report.location_id.in_(filter_ids),
            Report.timestamp >= since,
            Report.is_hidden == False,
            Report.employee_name.isnot(None),
        )
    )
    reports = result.scalars().all()

    # Группируем по имени сотрудника
    by_emp: dict[str, list] = {}
    for r in reports:
        by_emp.setdefault(r.employee_name, []).append(r)

    employees = []
    for name, rows in by_emp.items():
        total = len(rows)
        # Единый источник истины: финальный балл движка (fallback gpt_score для старых).
        scored = [(r.score if r.score is not None else r.gpt_score) for r in rows
                  if (r.score if r.score is not None else r.gpt_score) is not None]
        avg_score = round(sum(scored) / len(scored)) if scored else None
        sats = [r.customer_satisfaction for r in rows if r.customer_satisfaction]
        avg_sat = round(sum(sats) / len(sats), 1) if sats else None
        energies = [r.energy_level for r in rows if r.energy_level is not None]
        avg_energy = round(sum(energies) / len(energies), 1) if energies else None

        def cnt(flag):
            return sum(1 for r in rows if getattr(r, flag))

        def pct(flag):
            return round(cnt(flag) / total * 100) if total else 0

        employees.append({
            "name":           name,
            "total":          total,
            "avg_score":      avg_score,
            "avg_satisfaction": avg_sat,
            "avg_energy":     avg_energy,
            "positive_tone":  sum(1 for r in rows if r.tone == "positive"),
            "negative_tone":  sum(1 for r in rows if r.tone == "negative"),
            "neutral_tone":   sum(1 for r in rows if r.tone == "neutral"),
            "rude_count":     cnt("has_bad"),
            "fraud_count":    cnt("has_fraud"),
            "greetings_pct":  pct("has_greeting"),
            "goodbye_pct":    pct("has_goodbye"),
            "upsell_pct":     pct("has_bonus"),
        })

    # Сортируем: лучшие по среднему баллу сверху
    employees.sort(key=lambda e: (e["avg_score"] is not None, e["avg_score"] or 0), reverse=True)

    return {"employees": employees, "days": days}
