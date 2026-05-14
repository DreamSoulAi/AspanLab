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

import logging
import uuid
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Header, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.database import get_db, AsyncSessionLocal
from backend.models.location import Location
from backend.models.report import Report
from backend.models.alert import Alert
from backend.models.user import User
from backend.models.failed_job import FailedJob
from backend.services.analyzer import analyze, get_tone, calculate_score
from backend.services.audio_analyzer import analyze_audio_with_fallback
from backend.services.storage import upload_evidence
from backend.services.pos_matcher import match_report_with_pos
from backend.services.kaspi_detector import check_kaspi_fraud
from backend.services.evidence import create_evidence_clip
from backend.services.context_analyzer import analyze_context, check_pos_window
from backend.models.incident import Incident
from backend.services import notifier
from backend.api.auth import get_current_user
from backend.api.deps import get_location_by_api_key

log = logging.getLogger("reports")
router = APIRouter()

MAX_AUDIO_SIZE_MB = 10
RETRY_DIR = Path("uploads/retry")
RETRY_DIR.mkdir(parents=True, exist_ok=True)


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
) -> None:
    """
    Полный цикл обработки одного аудио-сегмента.
    """
    try:
        result = await analyze_audio_with_fallback(
            wav_bytes=wav_bytes,
            transcript_text=transcript_text,
            language=language,
        )

        # ── OpenAI не ответил → в очередь повторов ───────────────
        if not result:
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
            log.info(f"[loc={location_id}] IGNORE — нерабочий контент, пропущен")
            _mark_job_done(failed_job_id)
            return

        if not result.get("transcript"):
            log.info(f"[loc={location_id}] Речь не распознана")
            _mark_job_done(failed_job_id)
            return

        transcript = result["transcript"].strip()
        if len(transcript.split()) < 8:
            log.info(f"[loc={location_id}] Транскрипт < 8 слов — пропущен")
            _mark_job_done(failed_job_id)
            return

        # ── Поля GPT ─────────────────────────────────────────────
        speakers              = result.get("speakers", [])
        gpt_score             = result.get("score")
        gpt_summary           = result.get("summary", "")
        gpt_tone              = result.get("tone", "neutral")
        events                = result.get("events", {})
        priority              = int(result.get("priority", 0))
        payment_confirmed     = result.get("payment_confirmed")
        upsell_attempt        = result.get("upsell_attempt")
        customer_satisfaction = result.get("customer_satisfaction")

        # ── Contextual Severity: определяем контекст разговора ──
        # Нужен async-доступ к БД для проверки POS-окна
        async with AsyncSessionLocal() as _ctx_db:
            has_pos_nearby = await check_pos_window(location_id, datetime.utcnow(), _ctx_db)

        ctx = analyze_context(
            transcript=transcript,
            events=result.get("events", {}),
            speakers=result.get("speakers", []),
            has_pos_nearby=has_pos_nearby,
            customer_satisfaction=result.get("customer_satisfaction"),
            is_personal_talk=result.get("is_personal_talk", False),
        )
        conversation_context = ctx["context"]
        context_score        = ctx["score"]

        log.info(
            f"[loc={location_id}] context={conversation_context} "
            f"score={context_score:.2f} | {ctx['reason']}"
        )

        # ── priority=1: архив S3 + SHA-256 ───────────────────────
        audio_sha256 = s3_url = None
        if priority == 1 and wav_bytes:
            tmp_id         = int(datetime.utcnow().timestamp())
            storage_result = await upload_evidence(wav_bytes, location_id, tmp_id)
            audio_sha256   = storage_result.get("sha256")
            s3_url         = storage_result.get("s3_url")

        # ── Regex-анализ фраз ─────────────────────────────────────
        found = analyze(transcript, business_type=business_type, custom_phrases=custom_phrases or [])
        tone  = get_tone(found)
        score = calculate_score(found)

        has_greeting = ("✅ Приветствие"    in found) or events.get("greeting",      False)
        has_thanks   = ("✅ Благодарность"  in found)
        has_goodbye  = ("✅ Прощание"       in found) or events.get("farewell",      False)
        has_bonus    = ("⭐ Допродажа/бонус" in found) or events.get("upsell",        False) or bool(upsell_attempt)
        has_bad      = ("⚠️ Грубость"       in found) or events.get("rudeness",      False)
        has_fraud    = ("🚨 МОШЕННИЧЕСТВО"  in found) or events.get("fraud_attempt", False)

        is_priority_flag = bool(priority == 1 or has_fraud or has_bad)

        # ── Contextual Severity: понижаем приоритет для внутренних разговоров ──
        #
        # Если детектор определил что рядом НЕТ клиента (internal_talk)
        # И тумблер ignore_internal_profanity включён владельцем:
        #   → мат/конфликт пишем в БД с тегом internal_talk
        #   → но NOT отправляем Telegram-алерт (только тихий лог)
        #
        # Это принципиальная позиция продукта:
        # «Ребята матерятся между собой когда зал пуст — не наше дело.
        #  Но если клиент слышит мат — вы узнаете через секунду.»

        effective_tone = gpt_tone if gpt_tone in ("positive", "negative", "neutral") else tone
        tone_score_val = 1.0 if effective_tone == "positive" else 0.0 if effective_tone == "negative" else 0.5

        is_internal_talk = (conversation_context == "internal_talk")
        suppress_alert   = is_internal_talk and ignore_internal_profanity

        if suppress_alert:
            # Внутренний разговор сотрудников — глушим все флаги тревоги
            has_bad          = False
            has_fraud        = False
            is_priority_flag = False
            log.info(
                f"[loc={location_id}] INTERNAL_TALK: флаги подавлены "
                f"(ignore_internal_profanity=True). Записано в БД тихо."
            )

        hour = datetime.utcnow().hour
        shift_number = 1 if 6 <= hour < 14 else 2 if 14 <= hour < 22 else 3

        # ── Сохраняем в БД ────────────────────────────────────────
        async with AsyncSessionLocal() as db:
            report = Report(
                location_id=location_id,
                transcript=transcript,
                audio_size_kb=audio_size_kb,
                found_categories=found,
                has_greeting=has_greeting, has_thanks=has_thanks,
                has_goodbye=has_goodbye,   has_bonus=has_bonus,
                has_bad=has_bad,           has_fraud=has_fraud,
                tone=effective_tone,       tone_score=tone_score_val,
                shift_number=shift_number,
                gpt_score=gpt_score,       gpt_summary=gpt_summary,
                gpt_details={"positives": [], "issues": [], "events": events},
                speakers=speakers,
                is_priority=is_priority_flag,
                audio_sha256=audio_sha256,  s3_url=s3_url,
                payment_confirmed=payment_confirmed,
                upsell_attempt=upsell_attempt,
                customer_satisfaction=customer_satisfaction,
                is_personal_talk=False,
                is_hidden=False,
                fraud_status="normal",
                conversation_context=conversation_context,
                context_score=context_score,
            )
            db.add(report)
            await db.flush()

            # ── Kaspi Antifraud (до regex-Alert, чтобы исключить дублирование) ──
            # При internal_talk с включённым suppress_alert пропускаем — сотрудники говорят между собой
            kaspi_hits = [] if suppress_alert else check_kaspi_fraud(transcript, allowed_phones or [])
            for hit in kaspi_hits:
                evidence = {}
                if wav_bytes:
                    evidence = await create_evidence_clip(wav_bytes, location_id, report.id)
                incident = Incident(
                    location_id=location_id,
                    report_id=report.id,
                    incident_type="KASPI_FRAUD",
                    severity="critical",
                    description=(
                        f"Продавец продиктовал номер {hit['phone']}, "
                        f"которого нет в белом списке Каспи"
                    ),
                    detected_phone=hit["phone"],
                    proof_s3_url=evidence.get("s3_url"),
                    proof_sha256=evidence.get("sha256"),
                )
                db.add(incident)
                await db.flush()
                report.fraud_status = "critical_fraud_risk"
                report.is_priority  = True

                if telegram_chat:
                    await notifier.send_incident_alert(
                        chat_id=telegram_chat,
                        location_name=location_name,
                        incident_type="KASPI_FRAUD",
                        incident_id=incident.id,
                        description=incident.description,
                        proof_s3_url=incident.proof_s3_url,
                        detected_phone=hit["phone"],
                    )

            if kaspi_hits:
                await db.commit()
                # Kaspi уже создал Incident и отправил Telegram —
                # сбрасываем has_fraud чтобы не добавлять дублирующий Alert ниже
                has_fraud = False

            # Тревоги (regex)
            if has_fraud:
                db.add(Alert(location_id=location_id, report_id=report.id,
                             alert_type="fraud", severity="high", transcript=transcript,
                             trigger_phrase=", ".join(found.get("🚨 МОШЕННИЧЕСТВО", [])[:5])))
            if has_bad:
                db.add(Alert(location_id=location_id, report_id=report.id,
                             alert_type="bad_language", severity="high", transcript=transcript,
                             trigger_phrase=", ".join(found.get("⚠️ Грубость", [])[:5])))
            if effective_tone == "negative" and not has_bad:
                db.add(Alert(location_id=location_id, report_id=report.id,
                             alert_type="negative_tone", severity="medium", transcript=transcript))

            await db.commit()
            report_id = report.id

            # ── POS-матчинг если оплата подтверждена ─────────────
            if payment_confirmed:
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

        log.info(
            f"[loc={location_id}] Отчёт #{report_id} | "
            f"score={gpt_score} | tone={effective_tone} | "
            f"priority={priority} | payment={payment_confirmed} | "
            f"upsell={upsell_attempt} | sat={customer_satisfaction}"
        )

        # ── Уведомления ───────────────────────────────────────────
        if is_priority_flag and telegram_chat:
            await notifier.send_critical_alert({
                "telegram_chat": telegram_chat,
                "location_name": location_name,
                "summary":       gpt_summary,
                "audio_url":     s3_url,
                "sha256":        audio_sha256,
            })
        elif telegram_chat and (found or has_fraud or has_bad):
            # Нарушение — подробный отчёт
            await notifier.send_report(
                chat_id=telegram_chat, location_name=location_name,
                transcript=transcript, found=found,
                tone=effective_tone, score=score,
                audio_url=s3_url,
            )
        elif telegram_chat and not suppress_alert:
            # Обычный разговор — краткая сводка
            await notifier.send_ok_report(
                chat_id=telegram_chat,
                location_name=location_name,
                transcript=transcript,
                tone=effective_tone,
                score=score,
                upsell=upsell_attempt,
                greeting=has_greeting,
            )

        _mark_job_done(failed_job_id)

    except Exception:
        log.exception(f"[loc={location_id}] Ошибка фоновой обработки")
        if wav_bytes and not failed_job_id:
            await _enqueue_retry(
                location_id=location_id, wav_bytes=wav_bytes,
                language=language, audio_size_kb=audio_size_kb,
                business_type=business_type, custom_phrases=custom_phrases,
                telegram_chat=telegram_chat, location_name=location_name,
                error="Необработанное исключение",
            )


async def _enqueue_retry(
    location_id: int, wav_bytes: bytes,
    language: str, audio_size_kb: int,
    business_type: str, custom_phrases: list,
    telegram_chat: str, location_name: str,
    error: str = "",
):
    """Сохраняет аудио на диск и создаёт FailedJob для повтора через 5 минут."""
    try:
        fname = RETRY_DIR / f"{uuid.uuid4().hex}.wav"
        fname.write_bytes(wav_bytes)

        async with AsyncSessionLocal() as db:
            job = FailedJob(
                location_id=location_id,
                audio_path=str(fname),
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
        log.info(f"[loc={location_id}] Задача поставлена в очередь повторов: {fname.name}")
    except Exception as e:
        log.error(f"Не удалось сохранить в очередь повторов: {e}")


def _mark_job_done(job_id: Optional[int]):
    """Помечаем завершённую задачу из очереди повторов."""
    if not job_id:
        return
    import asyncio
    asyncio.create_task(_delete_job(job_id))


async def _delete_job(job_id: int):
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(FailedJob).where(FailedJob.id == job_id)
            )
            job = result.scalar()
            if job:
                # Удаляем аудио-файл
                if job.audio_path:
                    p = Path(job.audio_path)
                    if p.exists():
                        p.unlink(missing_ok=True)
                await db.delete(job)
                await db.commit()
    except Exception as e:
        log.error(f"Ошибка удаления FailedJob {job_id}: {e}")


# ── Endpoint: приём аудио ─────────────────────────────────────────────────────

@router.post("/submit")
async def submit_audio(
    background_tasks: BackgroundTasks,
    audio: Optional[UploadFile] = File(None),
    api_key:         Optional[str] = Form(None),
    x_api_key:       Optional[str] = Header(None),
    transcript_text: Optional[str] = Form(None),
    language:        Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Мгновенно принимает аудио и ставит в очередь GPT-обработки.
    """
    effective_key = (api_key or "").strip() or (x_api_key or "").strip()
    if not effective_key:
        raise HTTPException(status_code=401, detail="API ключ обязателен")

    location = await get_location_by_api_key(effective_key, db)
    location.last_seen = datetime.utcnow()

    wav_bytes: Optional[bytes] = None
    audio_size_kb = 0

    if audio:
        wav_bytes = await audio.read()
        size_mb = len(wav_bytes) / (1024 * 1024)
        if size_mb > MAX_AUDIO_SIZE_MB:
            raise HTTPException(status_code=413, detail=f"Файл > {MAX_AUDIO_SIZE_MB}MB")
        if len(wav_bytes) < 100:
            raise HTTPException(status_code=400, detail="Файл пустой или повреждён")
        audio_size_kb = len(wav_bytes) // 1024

    if not wav_bytes and not (transcript_text and transcript_text.strip()):
        raise HTTPException(status_code=400, detail="Нужно аудио или transcript_text")

    effective_language = language or location.language or "ru"
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
    )

    return {"status": "queued", "message": "Принято в обработку"}


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
            "transcript":            (r.transcript or "")[:300],
            "tone":                  r.tone,
            "gpt_score":             r.gpt_score,
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
            "s3_url":                r.s3_url,
            "audio_sha256":          r.audio_sha256,
        }
        for r in rows
    ]
