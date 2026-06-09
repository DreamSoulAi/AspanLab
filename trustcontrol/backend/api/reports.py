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
from backend.services.audio_analyzer import analyze_audio_with_fallback
from backend.services.storage import upload_evidence
from backend.services.pos_matcher import match_report_with_pos
from backend.services.kaspi_detector import check_kaspi_fraud
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


# ── Диагностика S3 / аудио-архива (для бесплатного Render без Shell) ──────────
@router.get("/_debug/s3")
async def debug_s3(token: str = ""):
    """Проверка S3 из браузера. Открыть:
       https://<домен>/api/reports/_debug/s3?token=<первые 12 симв SECRET_KEY>

    Грузит тестовый файл, проверяет публичную ссылку, смотрит s3_url
    у последних отчётов. Защищено префиксом SECRET_KEY.
    """
    from backend.config import settings
    if not token or not settings.SECRET_KEY or token != settings.SECRET_KEY[:12]:
        raise HTTPException(status_code=403, detail="Неверный токен (первые 12 символов SECRET_KEY)")

    out: dict = {"env": {}, "upload": {}, "public_url": {}, "recent_reports": []}

    out["env"] = {
        "S3_BUCKET":         settings.S3_BUCKET or None,
        "S3_ENDPOINT_URL":   settings.S3_ENDPOINT_URL or None,
        "S3_PUBLIC_URL":     settings.S3_PUBLIC_URL or None,
        "S3_REGION":         settings.S3_REGION or None,
        "AWS_ACCESS_KEY_ID": (settings.AWS_ACCESS_KEY_ID[:6] + "…") if settings.AWS_ACCESS_KEY_ID else None,
        "AWS_SECRET_set":    bool(settings.AWS_SECRET_ACCESS_KEY),
    }
    if not (settings.S3_BUCKET and settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY):
        out["error"] = "Не хватает ключей S3 в Environment"
        return out

    # Тестовый WAV: 1 секунда тишины
    import struct
    sr = 16000
    data = b"\x00\x00" * sr
    wav = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
           + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
           + b"data" + struct.pack("<I", len(data)) + data)

    # Прямая загрузка через boto3 с захватом РЕАЛЬНОЙ ошибки + перебор
    # вариантов checksum-конфига (botocore 1.36+ ломает Yandex).
    import boto3
    from botocore.config import Config
    import botocore

    _endpoint = (settings.S3_ENDPOINT_URL or "").strip() or None
    _region = "ru-central1" if (_endpoint and "yandexcloud" in _endpoint) else (settings.S3_REGION or "us-east-1")
    out["upload"]["botocore_version"] = botocore.__version__

    key = "evidence/_debug/test.wav"
    attempts = []

    # СНАЧАЛА проверка ключей на операции БЕЗ тела (нет checksum-фактора).
    # Если list падает с SignatureDoesNotMatch → виноваты КЛЮЧИ.
    # Если list проходит, а put падает → виноват checksum-баг botocore 1.36+.
    try:
        _probe = boto3.client(
            "s3", endpoint_url=_endpoint,
            aws_access_key_id=(settings.AWS_ACCESS_KEY_ID or "").strip(),
            aws_secret_access_key=(settings.AWS_SECRET_ACCESS_KEY or "").strip(),
            region_name=_region, config=Config(signature_version="s3v4"),
        )
        _probe.list_objects_v2(Bucket=settings.S3_BUCKET, MaxKeys=1)
        out["creds_check"] = {"list_objects": "OK — ключи и подпись валидны"}
    except Exception as e:
        out["creds_check"] = {"list_objects": f"{type(e).__name__}: {str(e)[:300]}"}

    configs = {
        "when_required": dict(signature_version="s3v4",
                              request_checksum_calculation="when_required",
                              response_checksum_validation="when_required"),
        "plain_s3v4":    dict(signature_version="s3v4"),
    }
    url = None
    for name, cfg_kwargs in configs.items():
        try:
            cfg = Config(**cfg_kwargs)
        except TypeError as te:
            attempts.append({"config": name, "error": f"Config не принял параметры: {te}"})
            continue
        try:
            s3 = boto3.client(
                "s3", endpoint_url=_endpoint,
                aws_access_key_id=(settings.AWS_ACCESS_KEY_ID or "").strip(),
                aws_secret_access_key=(settings.AWS_SECRET_ACCESS_KEY or "").strip(),
                region_name=_region, config=cfg,
            )
            s3.put_object(Bucket=settings.S3_BUCKET, Key=key, Body=wav, ContentType="audio/wav")
            if settings.S3_PUBLIC_URL:
                url = f"{settings.S3_PUBLIC_URL.rstrip('/')}/{key}"
            elif _endpoint:
                url = f"{_endpoint.rstrip('/')}/{settings.S3_BUCKET}/{key}"
            else:
                url = f"https://{settings.S3_BUCKET}.s3.{_region}.amazonaws.com/{key}"
            attempts.append({"config": name, "ok": True})
            break
        except Exception as e:
            attempts.append({"config": name, "error": f"{type(e).__name__}: {str(e)[:300]}"})

    out["upload"] = {"ok": bool(url), "url": url, "attempts": attempts,
                     "botocore_version": botocore.__version__}
    if not url:
        return out

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as cli:
            r = await cli.get(url)
        out["public_url"] = {"status": r.status_code, "bytes": len(r.content)}
        if r.status_code == 403:
            out["public_url"]["hint"] = "Файл загружен, но бакет НЕ публичный → браузер не откроет"
        elif r.status_code == 200:
            out["public_url"]["hint"] = "OK — аудио будет играть в браузере"
    except Exception as e:
        out["public_url"] = {"error": str(e)[:200]}

    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(Report).order_by(Report.timestamp.desc()).limit(5)
            )).scalars().all()
        out["recent_reports"] = [
            {"id": r.id, "time": r.timestamp.isoformat(), "has_s3_url": bool(r.s3_url)}
            for r in rows
        ]
    except Exception as e:
        out["recent_reports"] = {"error": str(e)[:200]}

    return out

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
) -> None:
    """
    Полный цикл обработки одного аудио-сегмента.
    """
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

        result = await analyze_audio_with_fallback(
            wav_bytes=wav_bytes,
            transcript_text=transcript_text,
            language=language,
            business_context=business_context,
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
        # Техническую диагностику STT в Telegram шлём ТОЛЬКО в DEBUG-режиме.
        # В проде клиент не должен видеть "🔧 STT: issai / timeout [yx=on...]" —
        # это спамило на каждый IGNORE. STT работает, отладка больше не нужна.
        if settings.DEBUG and telegram_chat and failed_job_id is None:
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
                    msg = f"🔧 STT: `{_eng}` / `{_stg}` {_extra}\n[yx={_y} issai={_i}{_kinfo}] {_status_line}\n{_preview}"
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
                await _send(telegram_chat, msg, reply_markup=_debug_listen)
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
            gpt_is_business=result.get("is_business", False),
        )
        conversation_context = ctx["context"]
        context_score        = ctx["score"]

        log.info(
            f"[loc={location_id}] context={conversation_context} "
            f"score={context_score:.2f} | {ctx['reason']}"
        )

        # ── Архив аудио в R2/S3 для прослушки + SHA-256 ──────────
        # Сохраняем КАЖДУЮ запись (не только priority=1), чтобы владелец
        # мог прослушать любой разговор (по s3_key строится публичная ссылка).
        # Если S3 не настроен — тихо пропускаем (upload_evidence вернёт s3_url=None).
        audio_sha256 = s3_url = s3_key = None
        if wav_bytes:
            tmp_id         = int(datetime.utcnow().timestamp())
            storage_result = await upload_evidence(wav_bytes, location_id, tmp_id)
            audio_sha256   = storage_result.get("sha256")
            s3_url         = storage_result.get("s3_url")
            s3_key         = storage_result.get("key")

        # ── Анализ фраз (regex резерв) + GPT events ──────────────
        found = analyze(transcript, business_type=business_type, custom_phrases=custom_phrases or [])

        # ── Фрод с порогом уверенности ───────────────────────────
        # GPT различает явный фрод (90-100) и косвенный намёк (50-89).
        # Тревогу и обнуление балла даём только при ВЫСОКОЙ уверенности —
        # один неуверенный сигнал не должен «разносить» кассира.
        fraud_confidence = int(result.get("fraud_confidence", 0) or 0)
        raw_fraud        = bool(events.get("fraud_attempt", False))
        has_fraud        = bool(raw_fraud and fraud_confidence >= FRAUD_HARD_THRESHOLD)   # явный
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
        # GPT-события (greeting/upsell/rudeness/…) уже учитывают track_*
        # на уровне наличия; здесь движок применяет настройки владельца
        # к итоговому баллу и не штрафует дважды.
        #
        # ВАЖНО: в балл отдаём события, ОБОГАЩЁННЫЕ regex-резервом. GPT нередко
        # пропускает явное «здравствуйте/спасибо/рахмет/сау болыңыз» в шумном
        # транскрипте и ставит greeting/farewell=false — тогда вежливый разговор
        # несправедливо застревает на базовых 60. Если вежливое слово реально
        # есть в тексте (regex его поймал) — засчитываем бонус. Это согласуется
        # с принципом «при сомнении трактуй в пользу кассира».
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
            # Внутренний разговор сотрудников — глушим все флаги тревоги
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

        # Кто был на кассе в это время (по сменам сотрудников)
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
                audio_sha256=audio_sha256,  s3_url=s3_url,  s3_key=s3_key,
                payment_confirmed=payment_confirmed,
                upsell_attempt=upsell_attempt,
                customer_satisfaction=customer_satisfaction,
                energy_level=energy_level,
                is_personal_talk=False,
                is_hidden=False,
                fraud_status="suspected" if fraud_suspect else "normal",
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
                # Email alert for fraud — send to location owner
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
                                )
                except Exception as _e:
                    log.warning(f"Fraud email not sent: {_e}")

            if kaspi_hits:
                await db.commit()
                # Kaspi уже создал Incident и отправил Telegram —
                # сбрасываем has_fraud чтобы не добавлять дублирующий Alert ниже
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
            report_saved = True   # отчёт в БД — повтор больше не нужен

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
            f"score={final_score} (gpt={gpt_score}) | tone={effective_tone} | "
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
        elif telegram_chat and (has_fraud or has_bad):
            await notifier.send_report(
                chat_id=telegram_chat, location_name=location_name,
                transcript=transcript, found=found,
                tone=effective_tone, score=final_score,
                audio_url=s3_url,
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
                audio_url=s3_url,
            )

        # Email for fraud incidents
        if (has_fraud or has_bad) and not suppress_alert:
            try:
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
                                audio_url=s3_url,
                            )
            except Exception as _e:
                log.warning(f"Fraud email not sent: {_e}")

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
            "s3_url":                r.s3_url,
            "audio_sha256":          r.audio_sha256,
            "has_audio":             bool(r.s3_key or r.s3_url),
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

    # Публичный бакет (R2 r2.dev) → прямая ссылка; иначе presigned на 1 час.
    from backend.config import settings
    if settings.S3_PUBLIC_URL:
        url = f"{settings.S3_PUBLIC_URL.rstrip('/')}/{report.s3_key}"
    else:
        url = presigned_get_url(report.s3_key, expires=3600)
    if not url:
        raise HTTPException(status_code=503, detail="Хранилище аудио не настроено")
    return {"url": url}
