# ════════════════════════════════════════════════════════════
#  Сервис: Russian STT — self-hosted faster-whisper (РУССКИЙ ГЕЙТ)
#  Модель: базовая мультиязычная whisper (хорошо понимает русский),
#          напр. openai/whisper-large-v3-turbo. Это ВТОРОЙ инстанс
#          того же worker/issai_worker.py с ISSAI_MODEL=<базовая модель>.
#
#  Роль в каскаде: НЕ финальный транскрипт, а БЕСПЛАТНЫЙ ГЕЙТ —
#  отсеять русскую болтовню кассиров/телефон/фон ДО платного OpenAI STT.
#  ISSAI делает то же для казахского; этот клиент — для русского.
#  Точные слова для фрод-вердикта всё равно даёт OpenAI (gpt-4o-transcribe).
#
#  Если RUSSIAN_WORKER_URL не задан — гейт молча пропускается,
#  каскад идёт в OpenAI как раньше.
# ════════════════════════════════════════════════════════════

import logging
import httpx
from backend.config import settings

log = logging.getLogger("russian_stt")


def is_enabled() -> bool:
    """Включён только если задан URL русского воркера."""
    return bool(settings.RUSSIAN_WORKER_URL)


async def transcribe(audio_bytes: bytes, diag: dict | None = None) -> str:
    """
    Отправляет аудио на self-hosted русский воркер, возвращает текст.

    Язык форсим "ru": этот воркер существует именно для русской речи (в отличие
    от ISSAI с lang="auto"). На казахской речи он даст слабый результат — но мы
    зовём его ТОЛЬКО когда ISSAI уже показал, что речь, вероятно, русская.

    Никогда не бросает исключение — при ошибке возвращает "", чтобы каскад
    спокойно перешёл к OpenAI STT.
    """
    if diag is None:
        diag = {}
    if not is_enabled():
        diag.update({"engine": "russian", "stage": "disabled"})
        return ""

    worker_url = settings.RUSSIAN_WORKER_URL.rstrip("/")
    headers = {}
    if settings.RUSSIAN_WORKER_KEY:
        headers["X-API-Key"] = settings.RUSSIAN_WORKER_KEY

    try:
        # 300с: CPU-инференция базовой whisper тяжелее ksc2, плюс очередь.
        async with httpx.AsyncClient(timeout=300.0) as cli:
            r = await cli.post(
                f"{worker_url}/transcribe",
                files={"audio": ("audio.wav", audio_bytes, "audio/wav")},
                data={"language": "ru"},
                headers=headers,
            )

        if r.status_code != 200:
            log.warning(f"Russian worker HTTP {r.status_code}: {r.text[:200]}")
            diag.update({"engine": "russian", "stage": "http_error",
                         "http": r.status_code, "error": r.text[:160]})
            return ""

        data = r.json()
        text = (data.get("text") or "").strip()
        words = len(text.split())
        if text:
            log.info(
                f"Russian STT OK | {len(text)} симв | {words} слов | {text[:80]!r}"
            )
            diag.update({"engine": "russian", "stage": "ok", "text": text[:160]})
        else:
            diag.update({"engine": "russian", "stage": "empty"})
        return text

    except httpx.TimeoutException:
        log.warning("Russian STT: таймаут — воркер не ответил за 300с")
        diag.update({"engine": "russian", "stage": "timeout",
                     "error": "воркер не ответил за 300с"})
        return ""
    except Exception as e:
        log.warning(f"Russian STT ошибка: {e}")
        diag.update({"engine": "russian", "stage": "connect_error",
                     "error": f"{type(e).__name__}: {str(e)[:160]}"})
        return ""
