# ════════════════════════════════════════════════════════════
#  API: Отчёты
#  SECURITY FIXES:
#  - Валидация размера аудио (макс 10MB)
#  - GET отчётов только своих точек
#  - Лимит на параметр limit (макс 200)
#  - API-ключ принимается из form-поля (нет проблем с latin-1)
#    с обратной совместимостью через X-API-Key заголовок
# ════════════════════════════════════════════════════════════

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Header, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime

from backend.database import get_db
from backend.models.location import Location
from backend.models.report import Report
from backend.models.alert import Alert
from backend.services.whisper import transcribe
from backend.services.analyzer import analyze, get_tone, calculate_score
from backend.services.gpt_analyzer import gpt_analyze
from backend.services import notifier
from backend.api.auth import get_current_user
from backend.models.user import User

router = APIRouter()

MAX_AUDIO_SIZE_MB = 10  # максимум 10MB


async def get_location_by_key(api_key: str, db: AsyncSession) -> Location:
    """Находим точку по API ключу."""
    result = await db.execute(
        select(Location).where(
            Location.api_key == api_key,
            Location.is_active == True
        )
    )
    loc = result.scalar()
    if not loc:
        raise HTTPException(status_code=401, detail="Неверный API ключ точки")
    return loc


@router.post("/submit")
async def submit_audio(
    audio: Optional[UploadFile] = File(None),
    # API-ключ: из form-поля (новый способ, нет latin-1) или из заголовка (старый)
    api_key: Optional[str] = Form(None),
    x_api_key: Optional[str] = Header(None),
    # Готовый транскрипт от воркера с faster-whisper (local mode)
    transcript_text: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Скрипт monitor.py отправляет сюда аудио или уже готовый транскрипт.

    Варианты вызова:
    1. Аудио + api_key (form field)  — новый способ, нет latin-1
    2. Аудио + X-API-Key (header)    — старый способ, обратная совместимость
    3. transcript_text + api_key     — local-whisper режим, дешевле
    """
    # Определяем API-ключ: form-поле имеет приоритет над заголовком
    effective_key = (api_key or "").strip() or (x_api_key or "").strip()
    if not effective_key:
        raise HTTPException(status_code=401, detail="API ключ обязателен (form: api_key или header: X-API-Key)")

    # Авторизация точки
    location = await get_location_by_key(effective_key, db)
    location.last_seen = datetime.utcnow()

    # ── Режим 1: получаем транскрипт от воркера (faster-whisper) ─
    if transcript_text and transcript_text.strip():
        transcript = transcript_text.strip()

    # ── Режим 2: получаем аудио и транскрибируем на сервере ──────
    elif audio:
        wav_bytes = await audio.read()
        size_mb = len(wav_bytes) / (1024 * 1024)
        if size_mb > MAX_AUDIO_SIZE_MB:
            raise HTTPException(
                status_code=413,
                detail=f"Файл слишком большой: {size_mb:.1f}MB. Максимум {MAX_AUDIO_SIZE_MB}MB"
            )
        if len(wav_bytes) < 100:
            raise HTTPException(status_code=400, detail="Файл пустой или повреждён")

        transcript = await transcribe(wav_bytes, language=location.language)
        if not transcript:
            return {"status": "silent", "message": "Речь не распознана"}
    else:
        raise HTTPException(status_code=400, detail="Нужно передать аудио-файл или transcript_text")

    audio_size_kb = len(wav_bytes) // 1024 if audio else 0

    # ── Анализ фраз и тона (regex) ───────────────────────────────
    found = analyze(
        transcript,
        business_type=location.business_type,
        custom_phrases=location.custom_phrases or [],
    )
    tone  = get_tone(found)
    score = calculate_score(found)

    # ── GPT-4o-mini анализ (AI-резюме) ───────────────────────────
    gpt_result  = await gpt_analyze(transcript)
    gpt_score   = gpt_result.get("score")
    gpt_summary = gpt_result.get("summary")
    gpt_details = {
        "positives": gpt_result.get("positives", []),
        "issues":    gpt_result.get("issues", []),
    } if gpt_result else None

    # ── Определяем смену ─────────────────────────────────────────
    hour = datetime.utcnow().hour
    if   6 <= hour < 14: shift_number = 1   # утро
    elif 14 <= hour < 22: shift_number = 2  # день
    else:                 shift_number = 3  # вечер/ночь

    # ── Сохраняем отчёт ──────────────────────────────────────────
    report = Report(
        location_id=location.id,
        transcript=transcript,
        audio_size_kb=audio_size_kb,
        found_categories=found,
        has_greeting = "✅ Приветствие"    in found,
        has_thanks   = "✅ Благодарность"  in found,
        has_goodbye  = "✅ Прощание"       in found,
        has_bonus    = "⭐ Допродажа/бонус" in found,
        has_bad      = "⚠️ Грубость"       in found,
        has_fraud    = "🚨 МОШЕННИЧЕСТВО"  in found,
        tone=tone,
        tone_score=1.0 if tone == "positive" else 0.0 if tone == "negative" else 0.5,
        shift_number=shift_number,
        gpt_score=gpt_score,
        gpt_summary=gpt_summary,
        gpt_details=gpt_details,
    )
    db.add(report)
    await db.flush()

    # ── Сохраняем тревоги ────────────────────────────────────────
    if "🚨 МОШЕННИЧЕСТВО" in found:
        db.add(Alert(
            location_id=location.id,
            report_id=report.id,
            alert_type="fraud",
            severity="high",
            transcript=transcript,
            trigger_phrase=", ".join(found.get("🚨 МОШЕННИЧЕСТВО", [])[:5]),
        ))

    if "⚠️ Грубость" in found:
        db.add(Alert(
            location_id=location.id,
            report_id=report.id,
            alert_type="bad_language",
            severity="high",
            transcript=transcript,
            trigger_phrase=", ".join(found.get("⚠️ Грубость", [])[:5]),
        ))

    if tone == "negative" and "⚠️ Грубость" not in found:
        db.add(Alert(
            location_id=location.id,
            report_id=report.id,
            alert_type="negative_tone",
            severity="medium",
            transcript=transcript,
        ))

    # ── Отправляем в Telegram ────────────────────────────────────
    chat_id = location.telegram_chat or (
        location.owner.telegram_chat if location.owner else None
    )
    if chat_id and found:
        await notifier.send_report(
            chat_id=chat_id,
            location_name=location.name,
            transcript=transcript,
            found=found,
            tone=tone,
            score=score,
        )

    return {
        "status":      "ok",
        "report_id":   report.id,
        "transcript":  transcript,
        "found":       list(found.keys()),
        "tone":        tone,
        "score":       score,
        "gpt_score":   gpt_score,
        "gpt_summary": gpt_summary,
    }


@router.get("/")
async def get_reports(
    location_id: int = None,
    has_fraud: bool = None,
    has_bad: bool = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Список отчётов. Пользователь видит только свои точки."""

    limit = min(limit, 200)

    locs_result = await db.execute(
        select(Location.id).where(Location.owner_id == user.id)
    )
    user_location_ids = [r[0] for r in locs_result.all()]

    if not user_location_ids:
        return []

    query = (
        select(Report)
        .where(Report.location_id.in_(user_location_ids))
        .order_by(Report.timestamp.desc())
        .limit(limit)
    )

    if location_id:
        if location_id not in user_location_ids:
            raise HTTPException(status_code=403, detail="Нет доступа к этой точке")
        query = query.where(Report.location_id == location_id)

    if has_fraud is not None:
        query = query.where(Report.has_fraud == has_fraud)
    if has_bad is not None:
        query = query.where(Report.has_bad == has_bad)

    result = await db.execute(query)
    reports = result.scalars().all()

    return [
        {
            "id":          r.id,
            "timestamp":   r.timestamp.isoformat(),
            "transcript":  r.transcript[:300],
            "tone":        r.tone,
            "score":       r.tone_score,
            "has_fraud":   r.has_fraud,
            "has_bad":     r.has_bad,
            "has_bonus":   r.has_bonus,
            "found":       list(r.found_categories.keys()) if r.found_categories else [],
            "gpt_score":   r.gpt_score,
            "gpt_summary": r.gpt_summary,
        }
        for r in reports
    ]
