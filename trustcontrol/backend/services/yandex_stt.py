# ════════════════════════════════════════════════════════════
#  Сервис: Yandex SpeechKit STT — распознавание казахского (kk-KZ)
#
#  ВАЖНО про казахский язык:
#  Казахский (kk-KZ) в Yandex SpeechKit доступен ТОЛЬКО в streaming и
#  delayed (асинхронном) режимах. Синхронный v1 stt:recognize казахский
#  НЕ поддерживает — он молча возвращает пусто/ошибку, и тогда казахская
#  речь уходит в аудио-модель OpenAI, которая её ПЕРЕВОДИТ на русский
#  вместо расшифровки. Поэтому здесь используется АСИНХРОННОЕ
#  распознавание (longRunningRecognize v2):
#
#    1. PCM из WAV заливаем во временный объект Object Storage
#    2. генерим presigned-ссылку (бакет приватный)
#    3. POST longRunningRecognize {uri, languageCode=kk-KZ}
#    4. опрашиваем операцию до done
#    5. склеиваем текст из chunks → alternatives
#    6. удаляем временный объект
#
#  Авторизация — API-ключ сервисного аккаунта (Api-Key).
#  Нужны переменные окружения:
#    YANDEX_STT_API_KEY   — API-ключ сервисного аккаунта (роль speechkit-stt)
#    YANDEX_STT_LANG      — kk-KZ (по умолчанию)
#    S3_BUCKET / S3_ENDPOINT_URL / AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
#      — тот же бакет Object Storage что и для архива (заливка временного аудио)
# ════════════════════════════════════════════════════════════

import io
import wave
import uuid
import asyncio
import logging

import httpx

from backend.config import settings

log = logging.getLogger("yandex_stt")

# v2 асинхронное (отложенное) распознавание — поддерживает казахский
_LRR_URL = "https://transcribe.api.cloud.yandex.net/speech/stt/v2/longRunningRecognize"
_OP_URL  = "https://operation.api.cloud.yandex.net/operations/"

# Yandex LPCM принимает только эти частоты дискретизации
_ALLOWED_RATES = (8000, 16000, 48000)

# Опрос операции: интервал и общий таймаут (≈10с обработки на 1 мин аудио)
_POLL_INTERVAL_SEC = 2.0
_POLL_TIMEOUT_SEC  = 120.0

# Время жизни presigned-ссылки на временное аудио
_PRESIGN_TTL_SEC = 1800


def is_enabled() -> bool:
    """
    Yandex STT включён только если есть API-ключ И настроен Object Storage
    (асинхронному распознаванию нужна заливка аудио в бакет).
    """
    return bool(
        settings.YANDEX_STT_API_KEY
        and settings.S3_BUCKET
        and settings.AWS_ACCESS_KEY_ID
        and settings.AWS_SECRET_ACCESS_KEY
    )


def _parse_wav(wav_bytes: bytes):
    """
    Разбирает WAV-контейнер.
    Возвращает (pcm_bytes, sample_rate) только для несжатого моно 16-bit PCM
    с поддерживаемой частотой. Иначе None.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            if wf.getcomptype() != "NONE":
                return None
            channels    = wf.getnchannels()
            sampwidth   = wf.getsampwidth()
            sample_rate = wf.getframerate()
            pcm = wf.readframes(wf.getnframes())
    except Exception:
        return None

    if sampwidth != 2:
        log.debug(f"Yandex: sampwidth={sampwidth} не 16-bit — пропуск")
        return None
    if channels != 1:
        log.debug(f"Yandex: channels={channels} не моно — пропуск")
        return None
    if sample_rate not in _ALLOWED_RATES:
        log.debug(f"Yandex: sample_rate={sample_rate} не поддерживается — пропуск")
        return None
    if not pcm:
        return None
    return pcm, sample_rate


def _build_s3_config():
    """
    Config для S3-совместимых хранилищ (Yandex/MinIO/Backblaze).

    botocore >= 1.36 по умолчанию добавляет CRC32-чексуммы к запросам,
    что НЕ-AWS хранилища не принимают → SignatureDoesNotMatch. Отключаем
    их (when_required). Старый botocore этих ключей не знает — фолбэк.
    """
    from botocore.config import Config
    base = {"signature_version": "s3v4"}
    try:
        return Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            **base,
        )
    except TypeError:
        return Config(**base)


def _s3_client():
    import boto3
    endpoint = (settings.S3_ENDPOINT_URL or "").strip() or None
    # Регион ОБЯЗАН совпадать с тем, что ждёт хранилище — иначе подпись s3v4
    # не сходится (SignatureDoesNotMatch). Для Yandex это всегда ru-central1.
    region = (settings.S3_REGION or "").strip() or "ru-central1"
    if endpoint and "yandexcloud" in endpoint:
        region = "ru-central1"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=(settings.AWS_ACCESS_KEY_ID or "").strip(),
        aws_secret_access_key=(settings.AWS_SECRET_ACCESS_KEY or "").strip(),
        region_name=region,
        config=_build_s3_config(),
    )


def _upload_and_presign(pcm: bytes, key: str) -> str:
    """Заливает PCM во временный объект и возвращает presigned GET-ссылку. (sync — звать через to_thread)"""
    s3 = _s3_client()
    s3.put_object(Bucket=settings.S3_BUCKET, Key=key, Body=pcm, ContentType="audio/x-pcm")
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET, "Key": key},
        ExpiresIn=_PRESIGN_TTL_SEC,
    )


def _delete_object(key: str) -> None:
    """Удаляет временный объект (sync — звать через to_thread). Ошибку глушим."""
    try:
        _s3_client().delete_object(Bucket=settings.S3_BUCKET, Key=key)
    except Exception:
        pass


async def transcribe(wav_bytes: bytes, lang: str | None = None, diag: dict | None = None) -> str:
    """
    Распознаёт речь (в т.ч. казахскую) через АСИНХРОННЫЙ Yandex SpeechKit.
    Возвращает текст или "" при любой ошибке/неподдержке (пайплайн уйдёт в фолбэк).

    diag — необязательный словарь для диагностики (заполняется по ходу):
      {"engine":"yandex","stage":..., "http":..., "error":..., "chars":...}
    """
    d = diag if diag is not None else {}
    d["engine"] = "yandex"

    if not is_enabled():
        d["stage"] = "disabled"
        return ""

    parsed = _parse_wav(wav_bytes)
    if not parsed:
        d["stage"] = "wav_unsupported"
        return ""
    pcm, sample_rate = parsed

    language = (lang or settings.YANDEX_STT_LANG or "kk-KZ").strip()
    key = f"stt-temp/{uuid.uuid4().hex}.pcm"

    # ── 1. Заливаем PCM в Object Storage и берём presigned-ссылку ──
    try:
        uri = await asyncio.to_thread(_upload_and_presign, pcm, key)
    except Exception as e:
        d["stage"] = "upload_failed"
        d["error"] = str(e)[:200]
        log.warning(f"Yandex STT: заливка в Object Storage не удалась: {e}")
        return ""

    headers = {"Authorization": f"Api-Key {settings.YANDEX_STT_API_KEY}"}
    body = {
        "config": {
            "specification": {
                "languageCode":      language,
                "model":             "general",
                "audioEncoding":     "LINEAR16_PCM",
                "sampleRateHertz":   sample_rate,
                "audioChannelCount": 1,
            }
        },
        "audio": {"uri": uri},
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as cli:
            # ── 2. Стартуем асинхронное распознавание ──
            r = await cli.post(_LRR_URL, headers=headers, json=body)
            if r.status_code != 200:
                d["stage"] = "lrr_http_error"
                d["http"]  = r.status_code
                d["error"] = r.text[:200]
                log.warning(f"Yandex STT longRunning HTTP {r.status_code}: {r.text[:200]}")
                return ""
            op_id = (r.json() or {}).get("id")
            if not op_id:
                d["stage"] = "no_operation_id"
                d["error"] = r.text[:200]
                return ""

            # ── 3. Опрашиваем операцию до done ──
            waited = 0.0
            op = {}
            while waited < _POLL_TIMEOUT_SEC:
                await asyncio.sleep(_POLL_INTERVAL_SEC)
                waited += _POLL_INTERVAL_SEC
                pr = await cli.get(_OP_URL + op_id, headers=headers)
                if pr.status_code != 200:
                    d["stage"] = "poll_http_error"
                    d["http"]  = pr.status_code
                    d["error"] = pr.text[:200]
                    log.warning(f"Yandex STT poll HTTP {pr.status_code}: {pr.text[:200]}")
                    return ""
                op = pr.json() or {}
                if op.get("done"):
                    break
            else:
                d["stage"] = "timeout"
                log.warning(f"Yandex STT: операция {op_id} не завершилась за {_POLL_TIMEOUT_SEC}с")
                return ""

            # Операция завершилась с ошибкой?
            if op.get("error"):
                d["stage"] = "operation_error"
                d["error"] = str(op["error"])[:200]
                log.warning(f"Yandex STT operation error: {op['error']}")
                return ""

            # ── 4. Склеиваем текст из chunks → alternatives ──
            chunks = ((op.get("response") or {}).get("chunks") or [])
            parts: list[str] = []
            for ch in chunks:
                for alt in (ch.get("alternatives") or []):
                    t = (alt.get("text") or "").strip()
                    if t:
                        parts.append(t)
            text = " ".join(parts).strip()
            d["stage"] = "ok" if text else "empty_result"
            d["chars"] = len(text)
            return text

    except Exception as e:
        d["stage"] = "exception"
        d["error"] = str(e)[:200]
        log.warning(f"Yandex STT запрос не удался: {e}")
        return ""
    finally:
        # ── 5. Чистим временный объект ──
        try:
            await asyncio.to_thread(_delete_object, key)
        except Exception:
            pass
