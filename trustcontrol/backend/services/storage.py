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


def _s3_client():
    """Создаёт boto3 S3-клиент (совместим с Cloudflare R2 / Supabase / MinIO / AWS)."""
    import boto3
    from botocore.config import Config
    from backend.config import settings

    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL or None,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.S3_REGION or "auto",
        # s3v4 обязателен для R2 и для корректных presigned-ссылок
        config=Config(signature_version="s3v4"),
    )


def presigned_get_url(key: str, expires: int = 3600) -> str | None:
    """
    Временная ссылка на прослушку записи (по умолчанию 1 час).
    Подписывается локально, без сетевого запроса. None если S3 не настроен.
    """
    from backend.config import settings

    if not (key and settings.S3_BUCKET and settings.AWS_ACCESS_KEY_ID):
        return None
    try:
        return _s3_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.S3_BUCKET, "Key": key},
            ExpiresIn=expires,
        )
    except Exception as e:
        log.error(f"presigned_get_url ошибка для {key}: {e}")
        return None


async def upload_evidence(audio_bytes: bytes, location_id: int, report_id: int) -> dict:
    """
    Архивирует аудио в S3/R2 (для прослушки и доказательств).

    Возвращает:
      {"sha256": "<hex>", "key": "<key>"}   — при успехе
      {"sha256": "<hex>", "key": None}       — если S3 не настроен/ошибка

    ВАЖНО (приватность): возвращаем ТОЛЬКО ключ объекта, а не публичную
    ссылку. Доступ к записи выдаётся исключительно через presigned-URL в
    эндпоинте get_report_audio (с проверкой прав владельца). Вечную
    публичную ссылку в коде не строим — даже если выставлен S3_PUBLIC_URL.

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
        return {"sha256": sha256, "key": None}

    try:
        import boto3  # noqa: F401
    except ImportError:
        log.error("boto3 не установлен: pip install boto3")
        return {"sha256": sha256, "key": None}

    from botocore.config import Config
    _endpoint = (settings.S3_ENDPOINT_URL or "").strip() or None
    _region = (settings.S3_REGION or "").strip() or "us-east-1"
    if _endpoint and "yandexcloud" in _endpoint:
        _region = "ru-central1"   # подпись s3v4 требует точный регион
    # botocore >= 1.36 шлёт CRC32-чексуммы, что ломает не-AWS хранилища
    # (SignatureDoesNotMatch). Отключаем их; старый botocore — фолбэк.
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

            log.info(f"[loc={location_id}] Архив S3 (попытка {attempt}): {key} | SHA256: {sha256[:16]}...")
            return {"sha256": sha256, "key": key}

        except Exception as e:
            last_err = e
            if attempt < _S3_MAX_RETRIES:
                wait = _S3_RETRY_BASE ** attempt
                log.warning(f"[loc={location_id}] S3 попытка {attempt} ошибка, повтор через {wait}s: {e}")
                await asyncio.sleep(wait)
            else:
                log.error(f"[loc={location_id}] S3 все {_S3_MAX_RETRIES} попытки исчерпаны: {last_err}")

    return {"sha256": sha256, "key": None}
