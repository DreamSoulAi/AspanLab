# ════════════════════════════════════════════════════════════
#  API: Отчёты  (V2.0 — Trust Control Optimized)
#
#  Ключевые улучшения:
#  - Фильтрация IGNORE (музыка/TikTok/шум) — не сохраняем мусор
#  - priority=1 → SHA-256 + архив в S3 + critical alert в Telegram
#  - API-ключ принимается из form-поля (нет проблем с latin-1)
#  - BackgroundTasks: принимаем мгновенно, GPT обрабатывает в фоне
# ════════════════════════════════════════════════════════════

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Header, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from datetime import datetime

from backend.database import get_db, AsyncSessionLocal
from backend.models.location import Location
from backend.models.report import Report
from backend.models.alert import Alert
from backend.models.user import User
from backend.services.analyzer import analyze, get_tone, calculate_score
from backend.services.audio_analyzer import analyze_audio_with_fallback
from backend.services.storage import upload_evidence
from backend.services import notifier
from backend.api.auth import get_current_user

log = logging.getLogger("reports")

router = APIRouter()

MAX_AUDIO_SIZE_MB = 10


async def get_location_by_key(api_key: str, db: AsyncSession) -> Location:
    """Находим точку по API ключу."""
    result = await db.execute(
        select(Location).where(
            Location.api_key == api_key,
            Location.is_active == True,
        )
    )
    loc = result.scalar()
    if not loc:
        raise HTTPException(status_code=401, detail="Неверный API ключ точки")
    return loc


# ── Фоновая обработка ────────────────────────────────────────────────────────

async def _process_submission(
    location_id: int,
    wav_bytes: Optional[bytes],
    transcript_text: Optional[str],
    language: Optional[str],
    audio_size_kb: int,
    business_type: Optional[str],
    custom_phrases: list,
    telegram_chat: Optional[str],
    location_name: str,
) -> None:
    """
    Запускается в фоне после того, как /submit вернул 200.
    Использует отдельную сессию БД — HTTP-сессия к этому моменту уже закрыта.

    Поток:
      1. GPT анализирует аудио → is_business, priority, transcript, summary
      2. IGNORE → тихо отбрасываем (музыка/TikTok/шум/нерабочий контент)
      3. priority=1 → SHA-256 + S3 архив + critical alert в Telegram
      4. Regex-анализ фраз → флаги (has_fraud, has_bad, ...)
      5. Сохраняем Report + Alert в БД
      6. Telegram уведомление при нарушениях
    """
    try:
        # ── GPT: транскрипция + анализ за один вызов ─────────────
        result = await analyze_audio_with_fallback(
            wav_bytes=wav_bytes,
            transcript_text=transcript_text,
            language=language,
        )

        if not result:
            log.info(f"[loc={location_id}] GPT вернул пустой результат — отчёт не создан")
            return

        # ── Фильтр мусора: IGNORE = не рабочий контент ───────────
        if result.get("status") == "IGNORE" or not result.get("is_business", True):
            log.info(
                f"[loc={location_id}] IGNORE — нерабочий контент "
                f"(музыка/TikTok/шум). Не сохраняем."
            )
            return

        if not result.get("transcript"):
            log.info(f"[loc={location_id}] Речь не распознана — отчёт не создан")
            return

        transcript = result["transcript"].strip()

        # Фильтр: минимум 8 слов — иначе это шум или случайный звук
        if len(transcript.split()) < 8:
            log.info(
                f"[loc={location_id}] Транскрипт слишком короткий "
                f"({len(transcript.split())} слов) — пропускаем"
            )
            return

        speakers    = result.get("speakers", [])
        gpt_score   = result.get("score")
        gpt_summary = result.get("summary", "")
        gpt_tone    = result.get("tone", "neutral")
        events      = result.get("events", {})
        priority    = int(result.get("priority", 0))

        # ── priority=1: архивируем в S3 с SHA-256 ────────────────
        audio_sha256 = None
        s3_url       = None

        if priority == 1 and wav_bytes:
            # report_id ещё неизвестен — используем временную метку
            tmp_report_id = int(datetime.utcnow().timestamp())
            storage_result = await upload_evidence(wav_bytes, location_id, tmp_report_id)
            audio_sha256   = storage_result.get("sha256")
            s3_url         = storage_result.get("s3_url")

        # ── Regex-анализ фраз (для флагов фильтрации) ────────────
        found = analyze(
            transcript,
            business_type=business_type,
            custom_phrases=custom_phrases or [],
        )
        tone  = get_tone(found)
        score = calculate_score(found)

        # Объединяем GPT events + regex flags
        has_greeting = ("✅ Приветствие"    in found) or events.get("greeting",      False)
        has_thanks   = ("✅ Благодарность"  in found)
        has_goodbye  = ("✅ Прощание"       in found) or events.get("farewell",      False)
        has_bonus    = ("⭐ Допродажа/бонус" in found) or events.get("upsell",        False)
        has_bad      = ("⚠️ Грубость"       in found) or events.get("rudeness",      False)
        has_fraud    = ("🚨 МОШЕННИЧЕСТВО"  in found) or events.get("fraud_attempt", False)

        # priority=1 также включается при детекции мошенничества/грубости
        is_priority = bool(priority == 1 or has_fraud or has_bad)

        # Тон: GPT приоритетнее regex
        effective_tone = gpt_tone if gpt_tone in ("positive", "negative", "neutral") else tone
        tone_score_val = (
            1.0 if effective_tone == "positive"
            else 0.0 if effective_tone == "negative"
            else 0.5
        )

        # ── Смена ─────────────────────────────────────────────────
        hour = datetime.utcnow().hour
        if   6 <= hour < 14:  shift_number = 1
        elif 14 <= hour < 22: shift_number = 2
        else:                 shift_number = 3

        # ── Сохраняем в БД ────────────────────────────────────────
        async with AsyncSessionLocal() as db:
            report = Report(
                location_id=location_id,
                transcript=transcript,
                audio_size_kb=audio_size_kb,
                found_categories=found,
                has_greeting=has_greeting,
                has_thanks=has_thanks,
                has_goodbye=has_goodbye,
                has_bonus=has_bonus,
                has_bad=has_bad,
                has_fraud=has_fraud,
                tone=effective_tone,
                tone_score=tone_score_val,
                shift_number=shift_number,
                gpt_score=gpt_score,
                gpt_summary=gpt_summary,
                gpt_details={
                    "positives": [],
                    "issues":    [],
                    "events":    events,
                },
                speakers=speakers,
                is_priority=is_priority,
                audio_sha256=audio_sha256,
                s3_url=s3_url,
            )
            db.add(report)
            await db.flush()

            # ── Тревоги ───────────────────────────────────────────
            if has_fraud:
                db.add(Alert(
                    location_id=location_id,
                    report_id=report.id,
                    alert_type="fraud",
                    severity="high",
                    transcript=transcript,
                    trigger_phrase=", ".join(found.get("🚨 МОШЕННИЧЕСТВО", [])[:5]),
                ))

            if has_bad:
                db.add(Alert(
                    location_id=location_id,
                    report_id=report.id,
                    alert_type="bad_language",
                    severity="high",
                    transcript=transcript,
                    trigger_phrase=", ".join(found.get("⚠️ Грубость", [])[:5]),
                ))

            if effective_tone == "negative" and not has_bad:
                db.add(Alert(
                    location_id=location_id,
                    report_id=report.id,
                    alert_type="negative_tone",
                    severity="medium",
                    transcript=transcript,
                ))

            await db.commit()
            report_id = report.id

        log.info(
            f"[loc={location_id}] Отчёт #{report_id} сохранён | "
            f"gpt_score={gpt_score} | tone={effective_tone} | priority={priority}"
        )

        # ── priority=1: критическое уведомление ──────────────────
        if is_priority and telegram_chat:
            await notifier.send_critical_alert({
                "telegram_chat": telegram_chat,
                "location_name": location_name,
                "summary":       gpt_summary,
                "audio_url":     s3_url,
                "sha256":        audio_sha256,
                "transcript":    transcript,
            })

        # ── Обычное Telegram уведомление при нарушениях ──────────
        elif telegram_chat and (found or has_fraud or has_bad):
            await notifier.send_report(
                chat_id=telegram_chat,
                location_name=location_name,
                transcript=transcript,
                found=found,
                tone=effective_tone,
                score=score,
            )

    except Exception:
        log.exception(f"[loc={location_id}] Ошибка фоновой обработки")


# ── Endpoint: приём аудио ─────────────────────────────────────────────────────

@router.post("/submit")
async def submit_audio(
    background_tasks: BackgroundTasks,
    audio: Optional[UploadFile] = File(None),
    # API-ключ: form-поле (новый способ — нет latin-1) или заголовок (обратная совместимость)
    api_key:    Optional[str] = Form(None),
    x_api_key:  Optional[str] = Header(None),
    # Готовый транскрипт от воркера с faster-whisper (local mode)
    transcript_text: Optional[str] = Form(None),
    language:        Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Мгновенно принимает аудио/транскрипт и ставит в очередь на обработку.
    Обработка через GPT идёт в фоне — ответ 200 возвращается сразу.

    Варианты вызова от monitor.py:
      1. Аудио + api_key (form field)  — основной способ
      2. Аудио + X-API-Key (header)    — обратная совместимость
      3. transcript_text + api_key     — local-whisper режим
    """
    effective_key = (api_key or "").strip() or (x_api_key or "").strip()
    if not effective_key:
        raise HTTPException(
            status_code=401,
            detail="API ключ обязателен (form: api_key или header: X-API-Key)",
        )

    location = await get_location_by_key(effective_key, db)
    location.last_seen = datetime.utcnow()

    # ── Читаем аудио (если есть) ──────────────────────────────
    wav_bytes: Optional[bytes] = None
    audio_size_kb = 0

    if audio:
        wav_bytes = await audio.read()
        size_mb = len(wav_bytes) / (1024 * 1024)
        if size_mb > MAX_AUDIO_SIZE_MB:
            raise HTTPException(
                status_code=413,
                detail=f"Файл слишком большой: {size_mb:.1f}MB. Максимум {MAX_AUDIO_SIZE_MB}MB",
            )
        if len(wav_bytes) < 100:
            raise HTTPException(status_code=400, detail="Файл пустой или повреждён")
        audio_size_kb = len(wav_bytes) // 1024

    if not wav_bytes and not (transcript_text and transcript_text.strip()):
        raise HTTPException(
            status_code=400,
            detail="Нужно передать аудио-файл или transcript_text",
        )

    effective_language = language or location.language or "ru"
    telegram_chat      = location.telegram_chat

    if not telegram_chat and location.owner_id:
        owner_result = await db.execute(
            select(User.telegram_chat).where(User.id == location.owner_id)
        )
        telegram_chat = owner_result.scalar()

    # ── Ставим в очередь ──────────────────────────────────────
    background_tasks.add_task(
        _process_submission,
        location_id=location.id,
        wav_bytes=wav_bytes,
        transcript_text=transcript_text,
        language=effective_language,
        audio_size_kb=audio_size_kb,
        business_type=location.business_type,
        custom_phrases=location.custom_phrases or [],
        telegram_chat=telegram_chat,
        location_name=location.name,
    )

    return {"status": "queued", "message": "Принято в обработку"}


# ── Endpoint: список отчётов ──────────────────────────────────────────────────

@router.get("/")
async def get_reports(
    location_id: int = None,
    has_fraud: bool = None,
    has_bad: bool = None,
    is_priority: bool = None,
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
    if is_priority is not None:
        query = query.where(Report.is_priority == is_priority)

    result = await db.execute(query)
    reports = result.scalars().all()

    return [
        {
            "id":           r.id,
            "timestamp":    r.timestamp.isoformat(),
            "transcript":   r.transcript[:300],
            "tone":         r.tone,
            "score":        r.tone_score,
            "has_fraud":    r.has_fraud,
            "has_bad":      r.has_bad,
            "has_bonus":    r.has_bonus,
            "found":        list(r.found_categories.keys()) if r.found_categories else [],
            "gpt_score":    r.gpt_score,
            "gpt_summary":  r.gpt_summary,
            "is_priority":  r.is_priority,
            "s3_url":       r.s3_url,
            "audio_sha256": r.audio_sha256,
        }
        for r in reports
    ]
