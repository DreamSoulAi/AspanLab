# ════════════════════════════════════════════════════════════
#  API: Отчёты  (v3.0 — Business Intelligence + Retry Queue)
#
#  Поток обработки:
#  1. GPT → is_business, priority, payment_confirmed, upsell_attempt,
#            customer_satisfaction, is_personal_talk
#  2. IGNORE  → тихо отбрасываем (мусор/шум)
#  3. PERSONAL → сохраняем с is_hidden=true (приватность сотрудника)
#  4. OK → полный анализ, флаги, алерты, S3 при priority=1
#  5. payment_confirmed=true → запуск POS-матчера
#  6. Ошибка OpenAI → FailedJob в очередь на повтор через 5 мин
# ════════════════════════════════════════════════════════════

import asyncio
import logging
import os
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Header, Form, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.config import settings
from backend.database import get_db, AsyncSessionLocal
from backend.models.location import Location
from backend.models.report import Report
from backend.models.alert import Alert
from backend.models.user import User
from backend.models.failed_job import FailedJob
from backend.services.analyzer import (
    analyze, get_tone, calculate_score,
    FRAUD_HARD_THRESHOLD, FRAUD_SOFT_THRESHOLD,
)
from backend.services.audio_analyzer import analyze_audio_with_fallback, extract_dictated_payment_number
from backend.services.stt_prompt import flatten_menu_glossary
from backend.services.storage import upload_evidence
from backend.services.pos_matcher import match_report_with_pos
from backend.services.kaspi_detector import (
    check_kaspi_fraud, has_transfer_intent, extract_phones, normalize_phone,
)
from backend.services.dialog_splitter import split_into_dialogues
from backend.services.evidence import create_evidence_clip
from backend.services.context_analyzer import analyze_context, check_pos_window
from backend.services.employee_matcher import match_employee
from backend.models.incident import Incident
from backend.services import notifier
from backend.services.subscription import get_status as get_sub_status
from backend.api.auth import get_current_user
from backend.api.deps import get_location_by_api_key

log = logging.getLogger("reports")
router = APIRouter()

MAX_AUDIO_SIZE_MB    = 10
MAX_TRANSCRIPT_CHARS = 10_000

# ── Потолок одновременной обработки ───────────────────────────────────────────
# Каждый разговор обрабатывается в фоне и может ждать STT (ISSAI) до 300с — всё
# это время фоновая задача жива. Без потолка под пиком задачи копятся десятками
# и разом конкурируют за коннекты к БД (free-tier Postgres = ~10 коннектов) →
# пул исчерпан → новые задачи 30с ждут свободный и падают с QueuePool TimeoutError
# (а заодно бьём в rate-limit STT). Семафор ограничивает число РЕАЛЬНО
# обрабатываемых разговоров; остальные ждут очереди в памяти. Касса при этом уже
# получила ok мгновенно — на неё это не влияет. Значение поднимается env-переменной
# вместе с пулом БД (DB_POOL_SIZE) при переезде на платную базу/воркер-очередь.
_MAX_CONCURRENT_PROCESSING = max(1, int(os.getenv("MAX_CONCURRENT_PROCESSING", "5")))
_PROCESS_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_PROCESSING)


# Per-API-key rate limits:
#   * 60 запросов / минуту  → защита от пиковых атак
#   * 1500 запросов / сутки → защита от runaway-кошелька OpenAI
#     (1500 = ~1 разговор/минуту 24/7 — заведомо больше любой реальной кассы)
_submit_attempts: dict[str, list[float]] = defaultdict(list)
_daily_counts:    dict[str, tuple[str, int]] = {}  # key → (YYYY-MM-DD, count)
MAX_SUBMITS_PER_MIN = 60
MAX_SUBMITS_PER_DAY = 1500

# Monthly conversation limits per plan
# Costs ~2₸/conversation on cheap pipeline → gross margin 75-85%
# These limits leave headroom for ads/sales team without going underwater
_PLAN_MONTHLY_LIMITS = {
    "trial":    150,      # 7-day trial enough to evaluate
    "start":    1500,     # 50/day - small kiosk/salon/shop
    "business": 3000,     # 100/day across 3 кассы - cafe/fastfood
    "potok":    7500,     # 250/day across 5 кассы - АЗС/supermarket
    "network":  999_999,  # individual, fair-use
}

# In-memory monthly count cache: user_id → (year_month_str, count, last_checked_ts)
# Refreshed from DB every 5 minutes to avoid per-request DB overhead
_monthly_cache: dict[int, tuple[str, int, float]] = {}
_CACHE_TTL = 300  # 5 minutes


async def _get_monthly_count(user_id: int, location_ids: list) -> int:
    """Returns conversation count for current month from cache or DB."""
    import time as _time
    from sqlalchemy import func
    now = _time.time()
    cur_month = datetime.utcnow().strftime("%Y-%m")
    cached = _monthly_cache.get(user_id)
    if cached and cached[0] == cur_month and now - cached[2] < _CACHE_TTL:
        return cached[1]
    # Refresh from DB
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(func.count(Report.id)).where(
                Report.location_id.in_(location_ids),
                Report.timestamp >= month_start,
                Report.is_hidden == False,
                # Биллинг «по записям»: доп. диалоги от умного разбиения одной
                # записи (is_primary=False) НЕ считаем — иначе лимит тарифа
                # расходовался бы в N раз быстрее на точках с очередью.
                Report.is_primary == True,
            )
        )
        count = result.scalar() or 0
    _monthly_cache[user_id] = (cur_month, count, now)
    return count


# Audio magic bytes — only accept real audio files
_AUDIO_MAGIC = [b"RIFF", b"ID3", b"OggS", b"fLaC", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"]


def _check_submit_rate(api_key: str):
    now = time.time()
    window_start = now - 60
    # Prune all stale keys from the dict to prevent memory growth
    stale = [k for k, v in _submit_attempts.items() if not v or max(v) < window_start]
    for k in stale:
        del _submit_attempts[k]

    recent = [t for t in _submit_attempts[api_key] if t > window_start]
    if len(recent) >= MAX_SUBMITS_PER_MIN:
        raise HTTPException(status_code=429, detail="Слишком много запросов от этой точки")
    recent.append(now)
    _submit_attempts[api_key] = recent

    # Дневной лимит (защита от runaway-расхода OpenAI)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    day, count = _daily_counts.get(api_key, (today, 0))
    if day != today:
        day, count = today, 0
    if count >= MAX_SUBMITS_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail="Превышен дневной лимит для этой точки (1500 разговоров)",
        )
    _daily_counts[api_key] = (day, count + 1)
RETRY_DIR = Path("uploads/retry")
RETRY_DIR.mkdir(parents=True, exist_ok=True)


# ── Сохранение одного диалога в Report (+ фрод, тревоги, уведомления) ──────────
# Вынесено из _process_submission, чтобы одну запись можно было разбить на
# НЕСКОЛЬКО диалогов (несколько клиентов) и сохранить отдельный Report на каждого.
# Аудио-архив (s3_key/sha256) и аудио-проба номера считаются ОДИН раз на запись
# в _process_submission и прокидываются сюда — здесь повторного аудио-вызова нет.
# Возвращает report_id или None (если сегмент пустой / нечего сохранять).
async def _persist_report(
    *,
    result: dict,
    transcript: str,
    location_id: int,
    audio_size_kb: int,
    business_type: Optional[str],
    allowed_phones: Optional[list],
    required_upsells: Optional[list],
    ignore_internal_profanity: bool,
    notify_ok_conversations: bool,
    track_upsell: bool,
    track_greeting: bool,
    track_goodbye: bool,
    employees: Optional[list],
    payment_mode: str,
    telegram_chat: Optional[str],
    location_name: str,
    wav_bytes: Optional[bytes],
    s3_key: Optional[str],
    audio_sha256: Optional[str],
    is_primary: bool = True,
    audio_fraud_number: Optional[str] = None,
) -> Optional[int]:
    transcript = (transcript or "").strip()
    word_count = len(transcript.split())
    if word_count < 2:
        return None

    # ── Поля GPT ─────────────────────────────────────────────
    speakers              = result.get("speakers") or []
    is_short = word_count < 6 or len(speakers) < 2
    gpt_score             = result.get("score")
    gpt_summary           = result.get("summary", "")
    gpt_tone              = result.get("tone", "neutral")
    events                = result.get("events", {})
    priority              = int(result.get("priority", 0))
    payment_confirmed     = result.get("payment_confirmed")
    upsell_attempt        = result.get("upsell_attempt")
    customer_satisfaction = result.get("customer_satisfaction")
    raw_energy            = result.get("energy_level")
    energy_level          = max(1, min(5, int(raw_energy))) if raw_energy is not None else None

    # ── Contextual Severity: определяем контекст разговора ──
    async with AsyncSessionLocal() as _ctx_db:
        has_pos_nearby = await check_pos_window(location_id, datetime.utcnow(), _ctx_db)

    ctx = analyze_context(
        transcript=transcript,
        events=result.get("events", {}),
        speakers=result.get("speakers", []),
        has_pos_nearby=has_pos_nearby,
        customer_satisfaction=result.get("customer_satisfaction"),
        is_personal_talk=result.get("is_personal_talk", False),
        gpt_is_business=result.get("is_business", False),
    )
    conversation_context = ctx["context"]
    context_score        = ctx["score"]
    log.info(
        f"[loc={location_id}] context={conversation_context} "
        f"score={context_score:.2f} | {ctx['reason']}"
    )

    # ── Анализ фраз (regex резерв) + GPT events ──────────────
    found = analyze(transcript, business_type=business_type)

    # ── Фрод с порогом уверенности ───────────────────────────
    fraud_confidence = int(result.get("fraud_confidence", 0) or 0)
    raw_fraud        = bool(events.get("fraud_attempt", False))
    has_fraud        = bool(raw_fraud and fraud_confidence >= FRAUD_HARD_THRESHOLD)
    fraud_suspect    = bool(raw_fraud and FRAUD_SOFT_THRESHOLD <= fraud_confidence < FRAUD_HARD_THRESHOLD)

    has_greeting = ("✅ Приветствие"   in found) or events.get("greeting", False)
    has_thanks   = ("✅ Благодарность" in found)
    has_goodbye  = ("✅ Прощание"      in found) or events.get("farewell", False)
    has_bonus    = events.get("upsell", False) or bool(upsell_attempt)
    has_bad      = events.get("rudeness", False)

    is_priority_flag = bool(priority == 1 or has_fraud or has_bad)

    # ── Тон ──────────────────────────────────────────────────
    effective_tone = get_tone(gpt_tone, events)
    tone_score_val = 1.0 if effective_tone == "positive" else 0.0 if effective_tone == "negative" else 0.5

    # ── Единый прозрачный движок оценки ──────────────────────
    score_events = dict(events)
    if has_greeting:
        score_events["greeting"] = True
    if has_goodbye or has_thanks:
        score_events["farewell"] = True

    final_score = calculate_score(
        events=score_events,
        tone=effective_tone,
        fraud_confidence=fraud_confidence,
        customer_satisfaction=customer_satisfaction,
        energy_level=energy_level,
        track_upsell=track_upsell,
        track_greeting=track_greeting,
        track_goodbye=track_goodbye,
        is_short=is_short,
    )

    is_internal_talk = (conversation_context == "internal_talk")
    suppress_alert   = is_internal_talk and ignore_internal_profanity

    if suppress_alert:
        has_bad          = False
        has_fraud        = False
        is_priority_flag = False
        log.info(
            f"[loc={location_id}] INTERNAL_TALK: флаги подавлены "
            f"(ignore_internal_profanity=True). Записано в БД тихо."
        )

    now_utc = datetime.utcnow()
    hour = now_utc.hour
    shift_number = 1 if 6 <= hour < 14 else 2 if 14 <= hour < 22 else 3
    employee_name = match_employee(employees, now_utc)

    # ── Сохраняем в БД ────────────────────────────────────────
    async with AsyncSessionLocal() as db:
        report = Report(
            location_id=location_id,
            transcript=transcript,
            employee_name=employee_name,
            audio_size_kb=audio_size_kb,
            found_categories=found,
            has_greeting=has_greeting, has_thanks=has_thanks,
            has_goodbye=has_goodbye,   has_bonus=has_bonus,
            has_bad=has_bad,           has_fraud=has_fraud,
            tone=effective_tone,       tone_score=tone_score_val,
            shift_number=shift_number,
            score=final_score,
            gpt_score=gpt_score,       gpt_summary=gpt_summary,
            gpt_details={"positives": result.get("positives", []), "issues": result.get("issues", []), "events": events},
            speakers=speakers,
            is_priority=is_priority_flag,
            audio_sha256=audio_sha256,  s3_key=s3_key,
            payment_confirmed=payment_confirmed,
            upsell_attempt=upsell_attempt,
            customer_satisfaction=customer_satisfaction,
            energy_level=energy_level,
            is_personal_talk=False,
            is_hidden=False,
            is_primary=is_primary,
            fraud_status="suspected" if fraud_suspect else "normal",
            conversation_context=conversation_context,
            context_score=context_score,
        )
        db.add(report)
        await db.flush()

        # ── Kaspi Antifraud (до regex-Alert, чтобы исключить дублирование) ──
        kaspi_hits = [] if suppress_alert else check_kaspi_fraud(
            transcript, allowed_phones or [], payment_mode
        )

        # Аудио-проба номера запускается ОДИН раз на всю запись (выше, в
        # _process_submission) и уже сверена с белым списком — здесь только
        # превращаем готовый номер в hit, без повторного аудио-вызова.
        if not suppress_alert and not kaspi_hits and audio_fraud_number:
            kaspi_hits = [{"phone": normalize_phone(audio_fraud_number)}]
            log.info(f"[loc={location_id}] Фрод: продиктован номер {normalize_phone(audio_fraud_number)} "
                     f"(из аудио, regex не достал) — нет в белом списке")

        for hit in kaspi_hits:
            evidence = {}
            if wav_bytes:
                evidence = await create_evidence_clip(wav_bytes, location_id, report.id)

            confidence = hit.get("confidence", "high")
            is_hard = (confidence == "high")

            incident = Incident(
                location_id=location_id,
                report_id=report.id,
                incident_type="KASPI_FRAUD" if is_hard else "KASPI_UNVERIFIED",
                severity="critical" if is_hard else "warning",
                description=(
                    f"Продавец продиктовал номер {hit['phone']}, "
                    f"которого нет в белом списке Каспи"
                    if is_hard else
                    f"Продавец продиктовал номер {hit['phone']} — "
                    f"белый список не настроен, требует проверки"
                ),
                detected_phone=hit["phone"],
                proof_s3_url=evidence.get("s3_url"),
                proof_sha256=evidence.get("sha256"),
            )
            db.add(incident)
            await db.flush()
            report.fraud_status = "critical_fraud_risk" if is_hard else "suspected"
            report.is_priority  = is_hard

            if telegram_chat:
                await notifier.send_incident_alert(
                    chat_id=telegram_chat,
                    location_name=location_name,
                    incident_type=incident.incident_type,
                    incident_id=incident.id,
                    description=incident.description,
                    proof_s3_url=incident.proof_s3_url,
                    detected_phone=hit["phone"],
                    report_id=report.id,
                )
            # Email только при жёстком обвинении (high confidence)
            if is_hard:
                try:
                    async with AsyncSessionLocal() as _mail_db:
                        _loc = await _mail_db.get(Location, location_id)
                        if _loc and _loc.owner_id:
                            _usr = await _mail_db.get(User, _loc.owner_id)
                            if _usr and _usr.email:
                                await notifier.send_fraud_email(
                                    user_email=_usr.email,
                                    location_name=location_name,
                                    incident_type="KASPI_FRAUD",
                                    description=incident.description,
                                    audio_url=incident.proof_s3_url,
                                    report_id=report.id,
                                )
                except Exception as _e:
                    log.warning(f"Fraud email not sent: {_e}")

        if kaspi_hits:
            await db.commit()
            has_fraud = False

        # Тревоги (regex)
        if has_fraud:
            db.add(Alert(location_id=location_id, report_id=report.id,
                         alert_type="fraud", severity="high", transcript=transcript,
                         trigger_phrase=gpt_summary[:150] if gpt_summary else "Обнаружено GPT-анализом"))
        if has_bad:
            db.add(Alert(location_id=location_id, report_id=report.id,
                         alert_type="bad_language", severity="high", transcript=transcript,
                         trigger_phrase=gpt_summary[:150] if gpt_summary else "Обнаружено GPT-анализом"))
        if effective_tone == "negative" and not has_bad:
            db.add(Alert(location_id=location_id, report_id=report.id,
                         alert_type="negative_tone", severity="medium", transcript=transcript))

        await db.commit()
        report_id = report.id

        # ── POS-матчинг если оплата подтверждена ─────────────
        # Best-effort: отчёт уже в БД, сбой матчинга не должен ронять сохранение.
        if payment_confirmed:
            try:
                new_fraud_status = await match_report_with_pos(
                    report, db, required_upsells=required_upsells or []
                )
                if new_fraud_status == "critical_fraud_risk":
                    report.fraud_status = new_fraud_status
                    report.is_priority  = True
                    db.add(Alert(location_id=location_id, report_id=report_id,
                                 alert_type="fraud", severity="high",
                                 transcript=transcript,
                                 trigger_phrase="POS-разрыв: нет чека в кассе"))
                    await db.commit()
            except Exception as _pe:
                log.warning(f"[loc={location_id}] POS-матчинг упал: {_pe}")

    log.info(
        f"[loc={location_id}] Отчёт #{report_id} (primary={is_primary}) | "
        f"score={final_score} (gpt={gpt_score}) | tone={effective_tone} | "
        f"priority={priority} | payment={payment_confirmed} | "
        f"upsell={upsell_attempt} | sat={customer_satisfaction}"
    )

    # ── Уведомления (best-effort, отчёт уже сохранён) ─────────
    try:
        from backend.services.storage import presigned_get_url
        listen_url = presigned_get_url(s3_key, expires=604800) if s3_key else None

        if is_priority_flag and telegram_chat:
            await notifier.send_critical_alert({
                "telegram_chat": telegram_chat,
                "location_name": location_name,
                "summary":       gpt_summary,
                "sha256":        audio_sha256,
                "report_id":     report_id,
                "audio_url":     listen_url,
            })
        elif telegram_chat and (has_fraud or has_bad):
            await notifier.send_report(
                chat_id=telegram_chat, location_name=location_name,
                transcript=transcript, found=found,
                tone=effective_tone, score=final_score,
                report_id=report_id, audio_url=listen_url,
            )
        elif telegram_chat and not suppress_alert and notify_ok_conversations:
            await notifier.send_ok_report(
                chat_id=telegram_chat,
                location_name=location_name,
                transcript=transcript,
                tone=effective_tone,
                score=final_score,
                upsell=upsell_attempt,
                greeting=has_greeting,
                summary=gpt_summary,
                report_id=report_id,
                audio_url=listen_url,
            )

        # Email for fraud incidents
        if (has_fraud or has_bad) and not suppress_alert:
            async with AsyncSessionLocal() as _mail_db:
                _loc = await _mail_db.get(Location, location_id)
                if _loc and _loc.owner_id:
                    _usr = await _mail_db.get(User, _loc.owner_id)
                    if _usr and _usr.email and has_fraud:
                        await notifier.send_fraud_email(
                            user_email=_usr.email,
                            location_name=location_name,
                            incident_type="FRAUD",
                            description=gpt_summary or "Обнаружено GPT-анализом",
                            report_id=report_id,
                        )
    except Exception as _ne:
        log.warning(f"[loc={location_id}] уведомление после сохранения упало: {_ne}")

    return report_id


# ── Фоновая обработка ─────────────────────────────────────────────────────────

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
    failed_job_id: Optional[int] = None,
    allowed_phones: Optional[list] = None,
    required_upsells: Optional[list] = None,
    ignore_internal_profanity: bool = False,
    ignore_background_media: bool = True,
    notify_ok_conversations: bool = False,
    business_description: Optional[str] = None,
    greeting_script: Optional[str] = None,
    upsell_script: Optional[str] = None,
    track_upsell: bool = True,
    track_greeting: bool = True,
    track_goodbye: bool = True,
    employees: Optional[list] = None,
    menu_json: Optional[list] = None,
    payment_mode: str = "mixed",
) -> None:
    """
    Полный цикл обработки одного аудио-сегмента.
    """
    # Потолок одновременной обработки (см. _PROCESS_SEMAPHORE): защита пула БД и
    # STT от пиковой нагрузки. Ждём слот тут, а не на стороне кассы (она уже
    # получила ok). Освобождаем строго в finally — даже при ошибке/отмене.
    await _PROCESS_SEMAPHORE.acquire()
    # Флаг: отчёт уже сохранён в БД. Если упадёт код ПОСЛЕ сохранения
    # (уведомления/email/диагностика) — НЕ ставим в очередь повторов,
    # иначе retry-воркер создаст идентичный дубль отчёта.
    report_saved = False
    try:
        # Собираем контекст бизнеса для GPT.
        # ВАЖНО: здесь только ДАННЫЕ точки. Вся ЛОГИКА как их трактовать
        # (норма тона по сфере, скрипт = ориентир а не шаблон, допродажа =
        # бонус а не штраф) живёт в промпте — едина для любого бизнеса.
        _type_names = {
            "coffee": "кофейня", "cafe": "кафе/ресторан", "fastfood": "фастфуд",
            "gas": "АЗС/заправка", "shop": "магазин/розница", "beauty": "салон красоты",
            "fitness": "фитнес-клуб", "hotel": "отель/гостиница",
            "pharmacy": "аптека", "clinic": "клиника/медцентр",
            "auto": "автосервис/автомойка", "service": "сфера услуг",
            "other": "другой бизнес",
        }
        business_context_parts = []
        if business_type:
            business_context_parts.append(f"Сфера бизнеса: {_type_names.get(business_type, business_type)}.")
        if business_description:
            business_context_parts.append(f"О точке: {business_description}")
        if greeting_script:
            business_context_parts.append(f"Ориентир приветствия/прощания (по смыслу, НЕ дословно): {greeting_script}")
        if upsell_script:
            business_context_parts.append(f"Желательные допродажи (бонус к оценке, НЕ обязанность): {upsell_script}")
        business_context = "\n".join(business_context_parts) or None

        # Плоский глоссарий для STT-промпта: custom_phrases + названия/размеры из меню.
        # business_context (описание, скрипты) → в GPT-анализ. Это → в транскрипцию.
        location_glossary = list(custom_phrases or []) + flatten_menu_glossary(menu_json)

        result = await analyze_audio_with_fallback(
            wav_bytes=wav_bytes,
            transcript_text=transcript_text,
            language=language,
            business_context=business_context,
            location_glossary=location_glossary or None,
            location_id=location_id,
        )

        # ── OpenAI не ответил → в очередь повторов ───────────────
        if not result:
            log.warning(f"[loc={location_id}] GPT вернул пустой результат — в очередь повторов")
            if wav_bytes:
                await _enqueue_retry(
                    location_id=location_id, wav_bytes=wav_bytes,
                    language=language, audio_size_kb=audio_size_kb,
                    business_type=business_type, custom_phrases=custom_phrases,
                    telegram_chat=telegram_chat, location_name=location_name,
                    error="GPT вернул пустой результат",
                )
            _mark_job_done(failed_job_id)
            return

        status         = result.get("status", "OK")
        is_personal    = result.get("is_personal_talk", False)
        transcript_raw = result.get("transcript", "")

        # ── Диагностика STT (временно): видно каким движком распознан текст ──
        # engine=yandex stage=ok → казахский распознан Yandex (правильно)
        # engine=audio_model      → казахского эталона не было, слова от OpenAI
        # stage=lrr_http_error/disabled/... → почему Yandex не сработал
        stt_diag = result.get("_stt_diag") or {}
        log.info(f"[loc={location_id}] STT diag: {stt_diag}")
        # Техническую диагностику STT шлём в ОТДЕЛЬНЫЙ админ-чат, НЕ владельцу.
        # Раньше "🔧 STT: issai / timeout ... Jaketini masita aldin" сыпалось в чат
        # клиента на каждый IGNORE и выглядело как сломанный продукт. Теперь это
        # уходит только если задан ADMIN_TELEGRAM_CHAT (твой личный отладочный чат);
        # пусто → диагностика только в логи, владелец видит лишь чистые отчёты.
        _diag_chat = settings.ADMIN_TELEGRAM_CHAT
        if _diag_chat and failed_job_id is None:
            try:
                from backend.services.notifier import _send, _listen_button
                from backend.services import yandex_stt, issai_stt
                _y = "on" if yandex_stt.is_enabled() else "OFF"
                _i = "on" if issai_stt.is_enabled() else "OFF"
                # Текст для превью: сначала из сохранённого транскрипта, иначе
                # из диагностики STT (на IGNORE отчёта нет, но что распозналось —
                # видно). Так понятно ЧТО услышал движок и почему отфильтровано.
                _preview = (transcript_raw or "").strip()[:400] or (stt_diag.get("text") or "")[:400]
                _status_line = f"status={status}" if status else ""
                if stt_diag:
                    _eng = stt_diag.get("engine", "?")
                    _stg = stt_diag.get("stage", "?")
                    _extra = stt_diag.get("error") or f"{stt_diag.get('http','')}"
                    _kl = stt_diag.get("keylen")
                    _ws = " +WS!" if stt_diag.get("key_had_ws") else ""
                    _kinfo = f" keylen={_kl}{_ws}" if _kl is not None else ""
                    # ── Сравнение движков: что выдал КАЖДЫЙ STT отдельно ──
                    # Видно насколько ISSAI (каз) и russian-гейт расходятся с
                    # gpt-4o-transcribe (openai) и каким вышел итоговый merge.
                    # Поля заполняются на каскадном пути; пустые — не показываем.
                    _cmp = []
                    _t_issai  = (stt_diag.get("issai")   or "").strip()
                    _t_ru     = (stt_diag.get("russian") or "").strip()
                    _t_openai = (stt_diag.get("openai")  or "").strip()
                    _t_merged = (stt_diag.get("merged")  or "").strip()
                    _t_gate   = (stt_diag.get("stt")     or "").strip()  # текст гейта при дропе
                    if _t_issai:
                        _cmp.append(f"🇰🇿 ISSAI: {_t_issai}")
                    if _t_ru:
                        _cmp.append(f"🇷🇺 Russian: {_t_ru}")
                    if _t_openai:
                        _cmp.append(f"🌐 gpt-4o: {_t_openai}")
                    if _t_merged:
                        _cmp.append(f"✅ Итог: {_t_merged}")
                    if not _cmp and _t_gate:
                        _cmp.append(f"🎚 Гейт: {_t_gate}")
                    _cmp_block = ("\n" + "\n".join(_cmp)) if _cmp else ""
                    msg = (f"🔧 STT: `{_eng}` / `{_stg}` {_extra}\n"
                           f"[yx={_y} issai={_i}{_kinfo}] {_status_line}"
                           f"{_cmp_block}\n{_preview}")
                else:
                    msg = f"🔧 STT: нет диагностики [yx={_y} issai={_i}] {_status_line}\n{_preview}"
                # Проверка слышимости: грузим СЫРОЕ аудио каждой записи в R2 и
                # вешаем кнопку «Слушать запись» — даже на IGNORE/пустой транскрипт.
                # Так на кассе слышно ЧТО реально ловит телефон (тихо/далеко/шум).
                _debug_listen = None
                if wav_bytes:
                    try:
                        _diag_id = int(datetime.utcnow().timestamp())
                        _up = await upload_evidence(wav_bytes, location_id, _diag_id)
                        _debug_listen = _listen_button(_up.get("s3_url"))
                    except Exception as _ue:
                        log.warning(f"[loc={location_id}] debug audio upload failed: {_ue}")
                await _send(_diag_chat, msg, reply_markup=_debug_listen)
            except Exception as _de:
                log.warning(f"[loc={location_id}] STT diag send failed: {_de}")

        log.info(
            f"[loc={location_id}] Pipeline | status={status!r} "
            f"| is_business={result.get('is_business')} "
            f"| is_personal={is_personal} "
            f"| transcript_words={len(transcript_raw.split())} "
            f"| summary={result.get('summary','')[:80]!r}"
        )

        # ── PERSONAL: скрытый личный разговор ────────────────────
        if status == "PERSONAL" or (status == "IGNORE" and is_personal):
            async with AsyncSessionLocal() as db:
                report = Report(
                    location_id=location_id,
                    transcript="[Личный разговор — скрыт]",
                    audio_size_kb=audio_size_kb,
                    is_hidden=True,
                    is_personal_talk=True,
                    found_categories={},
                    fraud_status="normal",
                )
                db.add(report)
                await db.commit()
            log.info(f"[loc={location_id}] PERSONAL — сохранено как is_hidden=true")
            _mark_job_done(failed_job_id)
            return

        # ── IGNORE: мусор — не сохраняем ─────────────────────────
        if status == "IGNORE" or not result.get("is_business", True):
            log.info(
                f"[loc={location_id}] IGNORE фильтр | status={status!r} "
                f"is_business={result.get('is_business')} "
                f"summary={result.get('summary','')[:80]!r}"
            )
            _mark_job_done(failed_job_id)
            return

        if not transcript_raw:
            log.info(f"[loc={location_id}] Речь не распознана — пустой транскрипт")
            _mark_job_done(failed_job_id)
            return

        transcript = transcript_raw.strip()
        word_count = len(transcript.split())
        # Совсем пусто (0-1 слово) — нечего сохранять. Но короткий визит
        # (клиент молча подал товар/чек, оплатил, ушёл с парой слов) — это
        # нормальная покупка для магазина/аптеки/АЗС: сохраняем как тихий
        # визит с нейтральным баллом, фиксируя что сказал кассир.
        if word_count < 2:
            log.info(
                f"[loc={location_id}] Транскрипт пустой ({word_count} слов) — пропущен: {transcript!r}"
            )
            _mark_job_done(failed_job_id)
            return
        # ── Архив аудио ОДИН раз на запись (общий для всех диалогов) ──
        # Сохраняем КАЖДУЮ запись (не только priority=1). Храним ТОЛЬКО s3_key —
        # ссылку выдаёт presigned-эндпоинт get_report_audio с проверкой прав.
        # Если запись режется на несколько диалогов — все Report ссылаются на
        # ОДНУ запись (по тексту аудио по сегментам не разрезать).
        audio_sha256 = s3_key = None
        if wav_bytes:
            tmp_id         = int(datetime.utcnow().timestamp())
            storage_result = await upload_evidence(wav_bytes, location_id, tmp_id)
            audio_sha256   = storage_result.get("sha256")
            s3_key         = storage_result.get("key")

        # ── Аудио-проба продиктованного номера — ОДИН раз на запись ──
        # Слушаем всё аудио единожды (дорого, но точно), номер сверяем с белым
        # списком, дальше прокидываем в нужный сегмент. Так нет N аудио-вызовов
        # и нет дубля инцидента по разным Report одной записи.
        audio_fraud_number = None
        _whole_events = result.get("events", {})
        _whole_raw_fraud = bool(_whole_events.get("fraud_attempt", False))
        if wav_bytes and (_whole_raw_fraud
                          or has_transfer_intent(transcript) or extract_phones(transcript)):
            _num = await extract_dictated_payment_number(wav_bytes, transcript)
            if _num:
                _allowed_norm = {normalize_phone(p) for p in (allowed_phones or [])}
                if normalize_phone(_num) not in _allowed_norm:
                    audio_fraud_number = _num

        _common = dict(
            location_id=location_id, audio_size_kb=audio_size_kb,
            business_type=business_type, allowed_phones=allowed_phones,
            required_upsells=required_upsells,
            ignore_internal_profanity=ignore_internal_profanity,
            notify_ok_conversations=notify_ok_conversations,
            track_upsell=track_upsell, track_greeting=track_greeting,
            track_goodbye=track_goodbye, employees=employees,
            payment_mode=payment_mode, telegram_chat=telegram_chat,
            location_name=location_name, wav_bytes=wav_bytes,
            s3_key=s3_key, audio_sha256=audio_sha256,
        )

        # ── Слой 2: умное разбиение записи на отдельные диалоги ──
        # Одна запись может содержать очередь клиентов + болтовню персонала.
        # split_into_dialogues режет ГОТОВЫЙ транскрипт через gpt-4o-mini.
        # Пустой список / один сегмент / ошибка → анализируем весь транскрипт
        # как раньше (фолбэк, ни один отчёт не теряется).
        segments = await split_into_dialogues(
            transcript, business_type=business_type,
            payment_mode=payment_mode, greeting_script=greeting_script,
        )

        if len(segments) >= 2:
            # Запись с очередью → отдельный Report на клиента.
            # Куда отнести продиктованный номер: первый сегмент с умыслом/номером.
            _fraud_seg_idx = None
            if audio_fraud_number:
                for _i, _s in enumerate(segments):
                    if has_transfer_intent(_s["text"]) or extract_phones(_s["text"]):
                        _fraud_seg_idx = _i
                        break
                if _fraud_seg_idx is None:
                    _fraud_seg_idx = 0

            any_saved = False
            for _i, seg in enumerate(segments):
                seg_text     = (seg.get("text") or "").strip()
                seg_type     = seg.get("type", "UNCLEAR")
                fraud_signal = has_transfer_intent(seg_text) or bool(extract_phones(seg_text))

                # PERSONAL без признака фрода — болтовня персонала: не создаём
                # Report и не платим за анализ (как и задумано). НО если в нём
                # звучит перевод/номер (сговор персонала) — анализируем.
                if seg_type == "PERSONAL" and not fraud_signal:
                    log.info(f"[loc={location_id}] сегмент#{_i} PERSONAL — пропущен (болтовня)")
                    continue

                # Текстовый ре-анализ сегмента (дёшево, без повторного аудио).
                try:
                    seg_result = await analyze_audio_with_fallback(
                        wav_bytes=None, transcript_text=seg_text,
                        language=language, business_context=business_context,
                        location_glossary=location_glossary or None,
                        location_id=location_id,
                    )
                except Exception as _ae:
                    log.warning(f"[loc={location_id}] сегмент#{_i} анализ упал: {_ae}")
                    seg_result = None

                _ok = (bool(seg_result)
                       and seg_result.get("status", "OK") not in ("IGNORE", "PERSONAL")
                       and seg_result.get("is_business", True)
                       and (seg_result.get("transcript") or "").strip())

                # Шум без признака фрода — пропускаем. Но при умысле/номере
                # (в т.ч. UNCLEAR/PERSONAL) ФОРСИМ анализ — фрод обязан ловиться.
                if not _ok and not fraud_signal:
                    continue
                if not _ok:
                    seg_result = {
                        "status": "OK", "is_business": True, "transcript": seg_text,
                        "summary": "Сегмент с признаком перевода (форс-проверка фрода)",
                        "tone": "neutral", "events": {}, "speakers": [],
                    }

                seg_num = audio_fraud_number if (_i == _fraud_seg_idx) else None
                try:
                    rid = await _persist_report(
                        result=seg_result,
                        transcript=(seg_result.get("transcript") or seg_text).strip(),
                        is_primary=(not any_saved),   # первый сохранённый = первичный (биллинг)
                        audio_fraud_number=seg_num,
                        **_common,
                    )
                except Exception as _pe:
                    log.warning(f"[loc={location_id}] сегмент#{_i} сохранение упало: {_pe}")
                    rid = None
                if rid:
                    any_saved = True
                    report_saved = True

            # Все сегменты отвалились → фолбэк на цельный отчёт (не теряем запись).
            if not any_saved:
                log.info(f"[loc={location_id}] split дал 0 сохранённых — фолбэк на цельный отчёт")
                rid = await _persist_report(
                    result=result, transcript=transcript,
                    is_primary=True, audio_fraud_number=audio_fraud_number, **_common,
                )
                if rid:
                    report_saved = True
        else:
            # Один диалог (или split отказался/упал) — один Report, как раньше.
            rid = await _persist_report(
                result=result, transcript=transcript,
                is_primary=True, audio_fraud_number=audio_fraud_number, **_common,
            )
            if rid:
                report_saved = True

        _mark_job_done(failed_job_id)

    except Exception as _exc:
        import traceback as _tb
        _err_text = _tb.format_exc()[-800:]
        log.exception(f"[loc={location_id}] Ошибка фоновой обработки")
        # Если отчёт уже сохранён — НЕ повторяем (иначе дубль).
        # Повтор нужен только когда упали ДО сохранения (STT/GPT/БД).
        if wav_bytes and not failed_job_id and not report_saved:
            await _enqueue_retry(
                location_id=location_id, wav_bytes=wav_bytes,
                language=language, audio_size_kb=audio_size_kb,
                business_type=business_type, custom_phrases=custom_phrases,
                telegram_chat=telegram_chat, location_name=location_name,
                error="Необработанное исключение",
            )
        if telegram_chat:
            try:
                from backend.services.notifier import _send
                await _send(
                    telegram_chat,
                    f"⚠️ *{location_name}* — ошибка обработки\\.\n"
                    f"```\n{_err_text}\n```",
                )
            except Exception:
                pass
    finally:
        _PROCESS_SEMAPHORE.release()


async def _enqueue_retry(
    location_id: int, wav_bytes: bytes,
    language: str, audio_size_kb: int,
    business_type: str, custom_phrases: list,
    telegram_chat: str, location_name: str,
    error: str = "",
):
    """Сохраняет аудио и создаёт FailedJob для повтора через 5 минут.

    Аудио кладём в R2 (постоянное хранилище) с ключом-маркером "r2:<key>",
    чтобы запись пережила засыпание/редеплой Render (локальный диск там
    эфемерный). Если R2 не настроен/недоступен — фолбэк на локальный диск
    (dev и деградация без потери логики).
    """
    try:
        from backend.services.storage import upload_retry_audio

        r2_key = await upload_retry_audio(wav_bytes, location_id)
        if r2_key:
            audio_path = f"r2:{r2_key}"
        else:
            fname = RETRY_DIR / f"{uuid.uuid4().hex}.wav"
            fname.write_bytes(wav_bytes)
            audio_path = str(fname)

        async with AsyncSessionLocal() as db:
            job = FailedJob(
                location_id=location_id,
                audio_path=audio_path,
                language=language,
                audio_size_kb=audio_size_kb,
                business_type=business_type,
                custom_phrases=custom_phrases or [],
                telegram_chat=telegram_chat,
                location_name=location_name,
                retry_count=0,
                next_retry_at=datetime.utcnow() + timedelta(minutes=5),
                last_error=error,
            )
            db.add(job)
            await db.commit()
        log.info(f"[loc={location_id}] Задача поставлена в очередь повторов: {audio_path}")
    except Exception as e:
        log.error(f"Не удалось сохранить в очередь повторов: {e}")


def _mark_job_done(job_id: Optional[int]):
    """Помечаем завершённую задачу из очереди повторов."""
    if not job_id:
        return
    import asyncio
    asyncio.create_task(_delete_job(job_id))


async def cleanup_retry_audio(audio_path: Optional[str]) -> None:
    """Удаляет аудио задачи из R2 (маркер r2:) или с локального диска.

    Зовётся при успешном повторе И при failed_permanently, чтобы мёртвые
    записи не копили место (в R2 — деньги, на диске — мусор).
    """
    if not audio_path:
        return
    if audio_path.startswith("r2:"):
        from backend.services.storage import delete_object
        await delete_object(audio_path[3:])
    else:
        p = Path(audio_path)
        if p.exists():
            p.unlink(missing_ok=True)


async def _delete_job(job_id: int):
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(FailedJob).where(FailedJob.id == job_id)
            )
            job = result.scalar()
            if job:
                await cleanup_retry_audio(job.audio_path)
                await db.delete(job)
                await db.commit()
    except Exception as e:
        log.error(f"Ошибка удаления FailedJob {job_id}: {e}")


# ── Endpoint: приём аудио ─────────────────────────────────────────────────────

@router.post("/submit")
async def submit_audio(
    background_tasks: BackgroundTasks,
    audio:           Optional[UploadFile] = File(None),
    x_api_key:       Optional[str]        = Header(None),
    transcript_text: Optional[str]        = Form(None),
    language:        Optional[str]        = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """Принимает аудио, отвечает сразу и обрабатывает в фоне (GPT + DB + Telegram).

    Обработка может занять 10-120с (асинхронный Yandex STT), поэтому НЕ держим
    HTTP-соединение открытым — иначе кнопка «Стоп» в PWA висит и пользователь
    шлёт повторный запрос (дубль). Возвращаем ok сразу, работаем фоном.
    """
    effective_key = (x_api_key or "").strip()
    if not effective_key:
        raise HTTPException(status_code=401, detail="API ключ обязателен (X-API-Key заголовок)")

    _check_submit_rate(effective_key)

    location = await get_location_by_api_key(effective_key, db)

    # ── Проверка подписки владельца ──────────────────────────────
    if location.owner_id:
        owner = await db.get(User, location.owner_id)
        if owner:
            sub_status = get_sub_status(owner)
            if sub_status == "blocked":
                raise HTTPException(
                    status_code=402,
                    detail="Подписка истекла. Оплатите в личном кабинете для возобновления.",
                )

            # Monthly conversation limit check
            plan = owner.plan or "trial"
            monthly_limit = _PLAN_MONTHLY_LIMITS.get(plan, _PLAN_MONTHLY_LIMITS["trial"])
            if monthly_limit < 999_999:
                locs_result = await db.execute(
                    select(Location.id).where(Location.owner_id == location.owner_id)
                )
                owner_loc_ids = [r[0] for r in locs_result.all()]
                monthly_used = await _get_monthly_count(location.owner_id, owner_loc_ids)
                if monthly_used >= monthly_limit:
                    raise HTTPException(
                        status_code=429,
                        detail=f"Месячный лимит {monthly_limit} разговоров исчерпан. "
                               f"Обновите тариф в личном кабинете.",
                    )

    location.last_seen = datetime.utcnow()
    await db.commit()

    wav_bytes: Optional[bytes] = None
    audio_size_kb = 0

    if audio:
        wav_bytes = await audio.read()
        size_mb = len(wav_bytes) / (1024 * 1024)
        if size_mb > MAX_AUDIO_SIZE_MB:
            raise HTTPException(status_code=413, detail=f"Файл > {MAX_AUDIO_SIZE_MB}MB")
        if len(wav_bytes) < 100:
            raise HTTPException(status_code=400, detail="Файл пустой или повреждён")
        # Validate audio magic bytes
        header = wav_bytes[:4]
        if not any(header[:len(m)] == m for m in _AUDIO_MAGIC):
            raise HTTPException(status_code=400, detail="Неверный формат аудио")
        audio_size_kb = len(wav_bytes) // 1024

    if transcript_text and len(transcript_text) > MAX_TRANSCRIPT_CHARS:
        raise HTTPException(status_code=400, detail=f"transcript_text не может быть длиннее {MAX_TRANSCRIPT_CHARS} символов")

    if not wav_bytes and not (transcript_text and transcript_text.strip()):
        raise HTTPException(status_code=400, detail="Нужно аудио или transcript_text")

    # language передаётся только в Whisper fallback (если audio-preview упал).
    # Не форсируем "ru" по умолчанию — пусть будет None, чтобы Whisper тоже авто-детектил.
    effective_language = language or location.language or None
    telegram_chat      = location.telegram_chat

    if not telegram_chat and location.owner_id:
        owner = await db.execute(select(User.telegram_chat).where(User.id == location.owner_id))
        telegram_chat = owner.scalar()

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
        allowed_phones=location.allowed_phones or [],
        required_upsells=location.required_upsells or [],
        ignore_internal_profanity=bool(location.ignore_internal_profanity),
        ignore_background_media=bool(getattr(location, "ignore_background_media", True)),
        notify_ok_conversations=bool(getattr(location, "notify_ok_conversations", False)),
        business_description=location.business_description,
        greeting_script=location.greeting_script,
        upsell_script=location.upsell_script,
        track_upsell=bool(getattr(location, 'track_upsell', True)),
        track_greeting=bool(getattr(location, 'track_greeting', True)),
        track_goodbye=bool(getattr(location, 'track_goodbye', True)),
        employees=getattr(location, 'employees', None) or [],
        menu_json=getattr(location, 'menu_json', None),
        payment_mode=getattr(location, 'payment_mode', None) or "mixed",
    )

    return {"status": "ok", "message": "Принято в обработку"}


# ── Endpoint: список отчётов ──────────────────────────────────────────────────

@router.get("/")
async def get_reports(
    location_id:  Optional[int]  = None,
    has_fraud:    Optional[bool] = None,
    has_bad:      Optional[bool] = None,
    is_priority:  Optional[bool] = None,
    fraud_status: Optional[str]  = None,
    include_hidden: bool = False,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Список отчётов. По умолчанию скрытые (личные) записи не показываются."""
    limit = min(limit, 200)

    locs = await db.execute(select(Location.id).where(Location.owner_id == user.id))
    user_locs = [r[0] for r in locs.all()]
    if not user_locs:
        return []

    query = (
        select(Report)
        .where(Report.location_id.in_(user_locs))
        .order_by(Report.timestamp.desc())
        .limit(limit)
    )

    if not include_hidden:
        query = query.where(Report.is_hidden == False)
    if location_id:
        if location_id not in user_locs:
            raise HTTPException(status_code=403, detail="Нет доступа к этой точке")
        query = query.where(Report.location_id == location_id)
    if has_fraud is not None:
        query = query.where(Report.has_fraud == has_fraud)
    if has_bad is not None:
        query = query.where(Report.has_bad == has_bad)
    if is_priority is not None:
        query = query.where(Report.is_priority == is_priority)
    if fraud_status:
        query = query.where(Report.fraud_status == fraud_status)

    result = await db.execute(query)
    rows = result.scalars().all()

    locs_r = await db.execute(
        select(Location.id, Location.name).where(Location.id.in_(user_locs))
    )
    loc_names = {r[0]: r[1] for r in locs_r.all()}

    return [
        {
            "id":                    r.id,
            "location_id":           r.location_id,
            "location_name":         loc_names.get(r.location_id, ""),
            "timestamp":             r.timestamp.isoformat(),
            "transcript":            r.transcript or "",
            "tone":                  r.tone,
            "gpt_score":             r.gpt_score,
            "score":                 r.score if r.score is not None else r.gpt_score,
            "employee_name":         r.employee_name,
            "gpt_summary":           r.gpt_summary,
            "has_greeting":          r.has_greeting,
            "has_thanks":            r.has_thanks,
            "has_goodbye":           r.has_goodbye,
            "has_fraud":             r.has_fraud,
            "has_bad":               r.has_bad,
            "has_bonus":             r.has_bonus,
            "is_priority":           r.is_priority,
            "fraud_status":          r.fraud_status,
            "payment_confirmed":     r.payment_confirmed,
            "upsell_attempt":        r.upsell_attempt,
            "customer_satisfaction": r.customer_satisfaction,
            "energy_level":          r.energy_level,
            "audio_sha256":          r.audio_sha256,
            "has_audio":             bool(r.s3_key),
        }
        for r in rows
    ]


@router.get("/{report_id}/audio")
async def get_report_audio(
    report_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Временная ссылка на прослушку записи разговора.
    Проверяет что отчёт принадлежит точке текущего владельца.
    """
    from backend.services.storage import presigned_get_url

    row = await db.execute(
        select(Report, Location.owner_id)
        .join(Location, Report.location_id == Location.id)
        .where(Report.id == report_id)
    )
    rec = row.first()
    if not rec or rec[1] != user.id:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    report = rec[0]
    if not report.s3_key:
        raise HTTPException(status_code=404, detail="Аудио для этого разговора не сохранено")

    # ПРИВАТНЫЙ доступ: всегда presigned-ссылка с коротким TTL (15 минут).
    # Права владельца уже проверены выше. Прямую вечную публичную ссылку
    # (S3_PUBLIC_URL) НЕ отдаём — она бы жила бессрочно без авторизации.
    url = presigned_get_url(report.s3_key, expires=900)
    if not url:
        raise HTTPException(status_code=503, detail="Хранилище аудио не настроено")
    return {"url": url}
