# ════════════════════════════════════════════════════════════
#  Сервис: сборщик обучающих пар (дистилляция знаний ISSAI ← OpenAI)
#
#  Что сохраняем: wav-файл в R2/S3 + метаданные в БД.
#  Когда: после каждого прохода OpenAI STT в каскаде — fire-and-forget.
#  Зачем: накопить датасет для LoRA fine-tuning ISSAI под домен клиента.
#
#  Включить: COLLECT_TRAINING_DATA=1 в .env
#  По умолчанию OFF — данные клиентов не копим без осознанного решения.
#
#  Схема хранения в R2:
#    training/{year}/{month}/{day}/{uuid8}.wav
#    metadata: location_id, business_context в S3 Metadata
# ════════════════════════════════════════════════════════════

import asyncio
import logging
import os
import uuid
from datetime import datetime

log = logging.getLogger("training_collector")

_COLLECT = os.getenv("COLLECT_TRAINING_DATA", "").strip().lower() in ("1", "true", "yes", "on")
_MIN_WORDS = 4      # минимум слов в openai_text чтобы считать пару значимой
_MAX_WAV_MB = 25    # не заливаем огромные файлы — что-то пошло не так


def is_enabled() -> bool:
    return _COLLECT


def _quality_ok(openai_text: str, audio_key: str | None) -> bool:
    """Пара пригодна для обучения: учитель написал достаточно и аудио есть."""
    if not openai_text or len(openai_text.split()) < _MIN_WORDS:
        return False
    return bool(audio_key)


async def _upload_wav(wav_bytes: bytes, location_id: int | None) -> str | None:
    """
    Загружает WAV в R2/S3, возвращает ключ объекта или None при ошибке/отсутствии R2.
    Переиспользует конфиг и паттерн из services/storage.py.
    """
    from backend.config import settings

    if not (settings.S3_BUCKET and settings.AWS_ACCESS_KEY_ID):
        return None

    if len(wav_bytes) > _MAX_WAV_MB * 1024 * 1024:
        log.warning(f"training_collector: файл {len(wav_bytes)//1024}KB > {_MAX_WAV_MB}MB — пропускаю")
        return None

    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        log.warning("boto3 не установлен — аудио не будет сохранено для обучения")
        return None

    try:
        _endpoint = (settings.S3_ENDPOINT_URL or "").strip() or None
        _region = (settings.S3_REGION or "").strip() or "us-east-1"
        if _endpoint and "yandexcloud" in _endpoint:
            _region = "ru-central1"
        _cfg_base = {"signature_version": "s3v4"}
        try:
            _cfg = Config(request_checksum_calculation="when_required",
                          response_checksum_validation="when_required", **_cfg_base)
        except TypeError:
            _cfg = Config(**_cfg_base)

        s3 = boto3.client(
            "s3",
            endpoint_url=_endpoint,
            aws_access_key_id=(settings.AWS_ACCESS_KEY_ID or "").strip(),
            aws_secret_access_key=(settings.AWS_SECRET_ACCESS_KEY or "").strip(),
            region_name=_region,
            config=_cfg,
        )

        ts  = datetime.utcnow().strftime("%Y/%m/%d")
        uid = uuid.uuid4().hex[:12]
        key = f"training/{ts}/{uid}.wav"

        meta = {}
        if location_id is not None:
            meta["location_id"] = str(location_id)

        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: s3.put_object(
                Bucket=settings.S3_BUCKET,
                Key=key,
                Body=wav_bytes,
                ContentType="audio/wav",
                Metadata=meta,
            ),
        )
        return key
    except Exception as e:
        log.warning(f"training_collector: ошибка загрузки в R2: {e}")
        return None


async def collect_pair(
    wav_bytes: bytes | None,
    openai_text: str,
    issai_text: str | None = None,
    merged_text: str | None = None,
    gpt_result: dict | None = None,
    business_context: str | None = None,
    location_id: int | None = None,
) -> None:
    """
    Fire-and-forget: сохраняет пару (аудио, текст учителя) для дообучения ISSAI.
    Вызывать через asyncio.create_task() — не ждать ответа в основном потоке.
    Никогда не бросает исключение.

    Логика качества:
      • если openai_text пустой или < 4 слов — строка создаётся с quality_ok=False
        (для статистики, но не для обучения);
      • если R2 не настроен — quality_ok=False (аудио-файл обязателен для обучения);
      • только quality_ok=True строки скрипт fine-tuning будет скачивать.
    """
    if not _COLLECT:
        return

    # Минимальная проверка до сети/БД — не тратить ресурсы на явный мусор
    if not openai_text or len(openai_text.split()) < 1:
        return

    try:
        gpt = gpt_result or {}

        # Загружаем аудио в R2 (если настроен и wav есть)
        audio_key = None
        if wav_bytes:
            audio_key = await _upload_wav(wav_bytes, location_id)

        quality = _quality_ok(openai_text, audio_key)

        # Пишем метаданные в БД
        from backend.database import AsyncSessionLocal
        from backend.models.training_sample import TrainingSample

        async with AsyncSessionLocal() as db:
            sample = TrainingSample(
                location_id=location_id,
                business_context=(business_context or "")[:100] or None,
                issai_text=issai_text or None,
                openai_text=openai_text,
                merged_text=merged_text or None,
                gpt_status=gpt.get("status") or None,
                gpt_is_business=gpt.get("is_business"),
                stt_engine=(gpt.get("_stt_diag") or {}).get("engine") or None,
                audio_key=audio_key,
                quality_ok=quality,
            )
            db.add(sample)
            await db.commit()
            log.info(
                f"training_collector: сохранена пара id={sample.id} "
                f"quality={quality} words={len(openai_text.split())} "
                f"audio={'да' if audio_key else 'нет'} "
                f"loc={location_id}"
            )

    except Exception as e:
        # Никогда не роняем основной поток из-за сборщика
        log.warning(f"training_collector: ошибка сохранения пары: {type(e).__name__}: {e}")
