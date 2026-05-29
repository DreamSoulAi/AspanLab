# ════════════════════════════════════════════════════════════
#  Сервис: Yandex SpeechKit STT — распознавание казахского (kk-KZ)
#
#  OpenAI (mini/full audio) слабо слышит чистый казахский — для него
#  это low-resource язык. Yandex SpeechKit обучен на казахском и даёт
#  заметно более точный текст. Используем его как ЭТАЛОН СЛОВ, а тон
#  голоса по-прежнему оценивает аудио-модель (см. audio_analyzer.py).
#
#  Используется синхронное распознавание v1 (stt:recognize):
#    • лимит одного запроса — 30 сек / 1 МБ
#    • длинные записи режем на сегменты и склеиваем текст
#    • формат отправки — сырой LPCM (16-bit PCM) из WAV-контейнера
#
#  Авторизация — API-ключ сервисного аккаунта (Api-Key), без IAM-токенов
#  (их пришлось бы обновлять каждые 12ч). Нужны два значения в .env:
#    YANDEX_STT_API_KEY   — ключ сервисного аккаунта
#    YANDEX_STT_FOLDER_ID — id каталога в Yandex Cloud
# ════════════════════════════════════════════════════════════

import io
import wave
import logging

import httpx

from backend.config import settings

log = logging.getLogger("yandex_stt")

_STT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"

# v1 sync: жёсткие лимиты 30 сек и 1 МБ на запрос. Режем с запасом.
_MAX_SEGMENT_SEC = 28
_MAX_BYTES       = 1_000_000

# Yandex LPCM принимает только эти частоты дискретизации
_ALLOWED_RATES = (8000, 16000, 48000)


def is_enabled() -> bool:
    """Yandex STT включён только если заданы оба секрета."""
    return bool(settings.YANDEX_STT_API_KEY and settings.YANDEX_STT_FOLDER_ID)


def _parse_wav(wav_bytes: bytes):
    """
    Разбирает WAV-контейнер.
    Возвращает (pcm_bytes, sample_rate) только для несжатого моно 16-bit PCM
    с поддерживаемой частотой. Иначе None (→ Yandex пропускаем, идёт фолбэк).
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            if wf.getcomptype() != "NONE":      # сжатый (не PCM)
                return None
            channels   = wf.getnchannels()
            sampwidth  = wf.getsampwidth()
            sample_rate = wf.getframerate()
            pcm = wf.readframes(wf.getnframes())
    except Exception:
        # Не WAV (mp3/ogg) или битый контейнер — не наш случай
        return None

    if sampwidth != 2:                          # поддерживаем только 16-bit
        log.debug(f"Yandex: sampwidth={sampwidth} не 16-bit — пропуск")
        return None
    if channels != 1:                           # только моно (PWA шлёт моно)
        log.debug(f"Yandex: channels={channels} не моно — пропуск")
        return None
    if sample_rate not in _ALLOWED_RATES:
        log.debug(f"Yandex: sample_rate={sample_rate} не поддерживается — пропуск")
        return None
    if not pcm:
        return None
    return pcm, sample_rate


def _segments(pcm: bytes, sample_rate: int):
    """Режет PCM на куски, влезающие в лимит v1 (по времени и по размеру)."""
    bytes_per_sec = sample_rate * 2             # моно 16-bit = 2 байта/сэмпл
    limit = min(_MAX_SEGMENT_SEC * bytes_per_sec, _MAX_BYTES)
    if limit % 2:                               # выравнивание по сэмплу (2 байта)
        limit -= 1
    for i in range(0, len(pcm), limit):
        yield pcm[i:i + limit]


async def transcribe(wav_bytes: bytes, lang: str | None = None) -> str:
    """
    Распознаёт речь через Yandex SpeechKit.
    Возвращает текст (склеенный по сегментам) или "" при любой ошибке/неподдержке.

    Никогда не бросает исключение наружу — на ошибке возвращает "",
    чтобы вызвавший код спокойно ушёл в фолбэк.
    """
    if not is_enabled():
        return ""

    parsed = _parse_wav(wav_bytes)
    if not parsed:
        return ""
    pcm, sample_rate = parsed

    language = (lang or settings.YANDEX_STT_LANG or "kk-KZ").strip()

    params = {
        "topic":           "general",
        "lang":            language,
        "folderId":        settings.YANDEX_STT_FOLDER_ID,
        "format":          "lpcm",
        "sampleRateHertz": str(sample_rate),
    }
    headers = {"Authorization": f"Api-Key {settings.YANDEX_STT_API_KEY}"}

    parts: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=30.0) as cli:
            for seg in _segments(pcm, sample_rate):
                if not seg:
                    continue
                r = await cli.post(_STT_URL, params=params, headers=headers, content=seg)
                if r.status_code != 200:
                    # 401 — неверный ключ, 400 — формат/лимит и т.п.
                    log.warning(f"Yandex STT HTTP {r.status_code}: {r.text[:200]}")
                    break
                try:
                    txt = (r.json().get("result") or "").strip()
                except Exception:
                    txt = ""
                if txt:
                    parts.append(txt)
    except Exception as e:
        log.warning(f"Yandex STT запрос не удался: {e}")
        return " ".join(parts).strip()

    return " ".join(parts).strip()
