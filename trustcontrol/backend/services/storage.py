# ════════════════════════════════════════════════════════════
#  Сервис: Облачный архив доказательств (S3 / Supabase Storage)
#
#  Вызывается когда GPT устанавливает priority=1 (конфликт/фрод).
#  Перед загрузкой генерирует SHA-256 хеш файла — это гарантия
#  того что запись не была изменена после архивирования
#  (доказательная база для разбора инцидентов).
# ════════════════════════════════════════════════════════════

import hashlib
import logging
from datetime import datetime

log = logging.getLogger("storage")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def upload_evidence(audio_bytes: bytes, location_id: int, report_id: int) -> dict:
    """
    Архивирует аудио-доказательство в S3/Supabase Storage.

    Возвращает:
      {"sha256": "<hex>", "s3_url": "<url>"}   — при успехе
      {"sha256": "<hex>", "s3_url": None}       — если S3 не настроен или ошибка

    SHA-256 вычисляется ДО загрузки и сохраняется в БД отдельно —
    даже если S3 недоступен хеш остаётся в отчёте как доказательство
    целостности оригинального файла.
    """
    from backend.config import settings

    sha256 = sha256_hex(audio_bytes)

    if not settings.S3_BUCKET:
        log.warning(
            f"[loc={location_id}] S3_BUCKET не задан — архивирование пропущено "
            f"(SHA256={sha256[:16]}... сохранён в БД)"
        )
        return {"sha256": sha256, "s3_url": None}

    try:
        import boto3
        from botocore.exceptions import ClientError

        s3 = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL or None,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.S3_REGION or "us-east-1",
        )

        ts  = datetime.utcnow().strftime("%Y/%m/%d")
        key = f"evidence/{ts}/loc{location_id}_r{report_id}_{sha256[:12]}.wav"

        s3.put_object(
            Bucket=settings.S3_BUCKET,
            Key=key,
            Body=audio_bytes,
            ContentType="audio/wav",
            Metadata={
                "sha256":      sha256,
                "location_id": str(location_id),
                "report_id":   str(report_id),
            },
        )

        if settings.S3_ENDPOINT_URL:
            url = f"{settings.S3_ENDPOINT_URL.rstrip('/')}/{settings.S3_BUCKET}/{key}"
        else:
            url = f"https://{settings.S3_BUCKET}.s3.{settings.S3_REGION}.amazonaws.com/{key}"

        log.info(
            f"[loc={location_id}] Архив S3: {key} | SHA256: {sha256[:16]}..."
        )
        return {"sha256": sha256, "s3_url": url}

    except ImportError:
        log.error("boto3 не установлен: pip install boto3")
        return {"sha256": sha256, "s3_url": None}
    except Exception as e:
        log.error(f"[loc={location_id}] Ошибка S3: {e}")
        return {"sha256": sha256, "s3_url": None}
