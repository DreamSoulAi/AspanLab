# ════════════════════════════════════════════════════════════
#  Сервис: ISSAI STT — self-hosted faster-whisper
#  Модель: abilmansplus/whisper-turbo-ksc2
#    • 9.16% WER на казахском (лучшая открытая модель)
#    • обучена на KSC2 (1 200 часов) + code-switching (шала-казахский)
#
#  Этот файл — HTTP-клиент к worker/issai_worker.py
#  Если ISSAI_WORKER_URL не задан — автоматически пропускается,
#  цепочка деградирует к Yandex SpeechKit / аудио-модели.
#
#  Приоритет в audio_analyzer.py:
#    ISSAI (self-hosted) → Yandex (облако) → аудио-модель OpenAI
# ════════════════════════════════════════════════════════════

import logging
import httpx
from backend.config import settings

log = logging.getLogger("issai_stt")


def is_enabled() -> bool:
    """Включён только если задан URL воркера."""
    return bool(settings.ISSAI_WORKER_URL)


async def transcribe(audio_bytes: bytes, lang: str | None = None, diag: dict | None = None) -> str:
    """
    Отправляет аудио на self-hosted ISSAI-воркер, возвращает текст.

    Поддерживает WAV, MP3, OGG, WebM — любой формат принимает ffmpeg на воркере.
    lang: код ISO (ru, kk, en…). Если None — берём из YANDEX_STT_LANG (kk-KZ → kk).
    diag: если передан dict — заполняется причиной ошибки (для Telegram-диагностики),
          чтобы было видно ПОЧЕМУ ISSAI не ответил (туннель мёртв / таймаут / HTTP).

    Никогда не бросает исключение — при ошибке возвращает "",
    чтобы вызывающий код перешёл к следующему STT в цепочке.
    """
    if diag is None:
        diag = {}
    if not is_enabled():
        diag.update({"engine": "issai", "stage": "disabled"})
        return ""

    # whisper-turbo-ksc2 принимает ISO-639-1: "kk", "ru", "en"
    # Используем "auto" чтобы модель сама определила язык —
    # в Алматы ~60% говорят на русском, принудительный kk ломал русскую речь.
    language = lang.split("-")[0].lower() if lang else "auto"

    worker_url = settings.ISSAI_WORKER_URL.rstrip("/")

    headers = {}
    if settings.ISSAI_WORKER_KEY:
        headers["X-API-Key"] = settings.ISSAI_WORKER_KEY

    try:
        # 300с: CPU-инференция ~80-90с на 40с аудио + очередь до 3 запросов.
        # Render всё равно может оборвать соединение на своём уровне, но
        # 120с было слишком мало даже для одного длинного разговора в очереди.
        async with httpx.AsyncClient(timeout=300.0) as cli:
            r = await cli.post(
                f"{worker_url}/transcribe",
                files={"audio": ("audio.wav", audio_bytes, "audio/wav")},
                data={"language": language},
                headers=headers,
            )

        if r.status_code != 200:
            log.warning(f"ISSAI worker HTTP {r.status_code}: {r.text[:200]}")
            diag.update({"engine": "issai", "stage": "http_error",
                         "http": r.status_code, "error": r.text[:160]})
            return ""

        data = r.json()
        text = (data.get("text") or "").strip()
        if text:
            log.info(
                f"ISSAI STT OK | lang={data.get('language', language)} "
                f"| {len(text)} симв | {text[:80]!r}"
            )
            diag.update({"engine": "issai", "stage": "ok", "text": text[:160]})
        else:
            log.info("ISSAI STT: пустой ответ")
            diag.update({"engine": "issai", "stage": "empty"})
        return text

    except httpx.TimeoutException:
        log.warning("ISSAI STT: таймаут — воркер не ответил за 300с")
        diag.update({"engine": "issai", "stage": "timeout",
                     "error": "воркер не ответил за 300с"})
        return ""
    except Exception as e:
        log.warning(f"ISSAI STT ошибка: {e}")
        diag.update({"engine": "issai", "stage": "connect_error",
                     "error": f"{type(e).__name__}: {str(e)[:160]}"})
        return ""
