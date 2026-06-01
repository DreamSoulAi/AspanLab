# ════════════════════════════════════════════════════════════
#  Сервис: Yandex SpeechKit STT — распознавание казахского (kk-KZ)
#
#  ВАЖНО про казахский язык:
#  Казахский (kk-KZ) в Yandex SpeechKit доступен ТОЛЬКО в streaming и
#  delayed (асинхронном) режимах. Синхронный v1 stt:recognize казахский
#  НЕ поддерживает — он молча возвращает пусто/ошибку, и тогда казахская
#  речь уходит в аудио-модель OpenAI, которая её ПЕРЕВОДИТ на русский
#  вместо расшифровки. Поэтому здесь используется АСИНХРОННОЕ
#  распознавание (longRunningRecognize v2) с inline base64-аудио.
#
#  Нужные переменные окружения:
#    YANDEX_STT_API_KEY   — API-ключ сервисного аккаунта (роль speechkit-stt)
#    YANDEX_STT_FOLDER_ID — id каталога (необязателен если аккаунт в одном каталоге)
#    YANDEX_STT_LANG      — kk-KZ (по умолчанию)
# ════════════════════════════════════════════════════════════

import io
import wave
import base64
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

# Максимальный PCM для inline-загрузки (8 МБ ≈ 4 мин @ 16кГц 16бит моно)
_MAX_PCM_BYTES = 8 * 1024 * 1024


def is_enabled() -> bool:
    """Yandex STT включён если задан API-ключ."""
    return bool(settings.YANDEX_STT_API_KEY)


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


async def transcribe(wav_bytes: bytes, lang: str | None = None, diag: dict | None = None) -> str:
    """
    Распознаёт речь (в т.ч. казахскую) через АСИНХРОННЫЙ Yandex SpeechKit.
    Аудио передаётся как base64 inline — без загрузки в Object Storage.
    Возвращает текст или "" при любой ошибке/неподдержке.

    diag — необязательный словарь для диагностики:
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

    if len(pcm) > _MAX_PCM_BYTES:
        d["stage"] = "pcm_too_large"
        log.warning(f"Yandex STT: PCM {len(pcm)//1024}KB > {_MAX_PCM_BYTES//1024//1024}MB — пропуск")
        return ""

    language = (lang or settings.YANDEX_STT_LANG or "kk-KZ").strip()
    content_b64 = base64.b64encode(pcm).decode("ascii")

    headers = {"Authorization": f"Api-Key {settings.YANDEX_STT_API_KEY}"}
    body: dict = {
        "config": {
            "specification": {
                "languageCode":      language,
                "model":             "general",
                "audioEncoding":     "LINEAR16_PCM",
                "sampleRateHertz":   sample_rate,
                "audioChannelCount": 1,
            }
        },
        "audio": {"content": content_b64},
    }
    if settings.YANDEX_STT_FOLDER_ID:
        body["folderId"] = settings.YANDEX_STT_FOLDER_ID

    try:
        async with httpx.AsyncClient(timeout=30.0) as cli:
            # ── 1. Стартуем асинхронное распознавание ──
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

            # ── 2. Опрашиваем операцию до done ──
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

            # ── 3. Склеиваем текст из chunks → alternatives ──
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
