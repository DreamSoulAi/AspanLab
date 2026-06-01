# ════════════════════════════════════════════════════════════
#  Сервис: Облачный архив доказательств (S3 / Supabase Storage)
# ════════════════════════════════════════════════════════════

import asyncio
import hashlib
import logging
from datetime import datetime

log = logging.getLogger("storage")

_S3_MAX_RETRIES = 3
_S3_RETRY_BASE  = 2   # секунды: 2s, 4s, 8s


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def upload_evidence(audio_bytes: bytes, location_id: int, report_id: int) -> dict:
    """
    Архивирует аудио-доказательство в S3/Supabase Storage.

    Возвращает:
      {"sha256": "<hex>", "s3_url": "<url>"}   — при успехе
      {"sha256": "<hex>", "s3_url": None}       — если S3 не настроен или все попытки исчерпаны

    SHA-256 вычисляется ДО загрузки — даже если S3 недоступен хеш
    остаётся в БД как доказательство целостности файла.
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
    except ImportError:
        log.error("boto3 не установлен: pip install boto3")
        return {"sha256": sha256, "s3_url": None}

    from botocore.config import Config
    # .strip() — частая причина SignatureDoesNotMatch: пробел/перенос в секрете.
    s3 = boto3.client(
        "s3",
        endpoint_url=(settings.S3_ENDPOINT_URL or "").strip() or None,
        aws_access_key_id=(settings.AWS_ACCESS_KEY_ID or "").strip(),
        aws_secret_access_key=(settings.AWS_SECRET_ACCESS_KEY or "").strip(),
        region_name=(settings.S3_REGION or "us-east-1").strip(),
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )

    ts  = datetime.utcnow().strftime("%Y/%m/%d")
    key = f"evidence/{ts}/loc{location_id}_r{report_id}_{sha256[:12]}.wav"

    last_err = None
    for attempt in range(1, _S3_MAX_RETRIES + 1):
        try:
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

            log.info(f"[loc={location_id}] Архив S3 (попытка {attempt}): {key} | SHA256: {sha256[:16]}...")
            return {"sha256": sha256, "s3_url": url}

        except Exception as e:
            last_err = e
            if attempt < _S3_MAX_RETRIES:
                wait = _S3_RETRY_BASE ** attempt
                log.warning(f"[loc={location_id}] S3 попытка {attempt} ошибка, повтор через {wait}s: {e}")
                await asyncio.sleep(wait)
            else:
                log.error(f"[loc={location_id}] S3 все {_S3_MAX_RETRIES} попытки исчерпаны: {last_err}")

    return {"sha256": sha256, "s3_url": None}
