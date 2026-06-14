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
import os
import httpx
from backend.config import settings

log = logging.getLogger("issai_stt")

# Таймаут запроса к воркеру. CPU-инференция ~80-90с на 40с аудио + очередь.
# 300с по умолчанию (длинный казахский разговор в очереди успевает), но если
# воркер регулярно лежит/перегружен — снижай env ISSAI_TIMEOUT, чтобы быстрее
# падать в фолбэк и не держать слот обработки (см. семафор в reports.py).
_ISSAI_TIMEOUT = float(os.getenv("ISSAI_TIMEOUT", "300"))


def is_enabled() -> bool:
    """Включён только если задан URL воркера."""
    return bool(settings.ISSAI_WORKER_URL)


def is_garbage(text: str, audio_duration: float) -> bool:
    """
    Похоже ли на мусор: на длинном аудио (>=12с речи) распознаватель отдал
    меньше 4 слов. Чаще всего это провал модели (русская речь через казахскую
    модель → каша). Чистая функция — вынесена для тестируемости.
    """
    if not text:
        return False
    return audio_duration >= 12 and len(text.split()) < 4


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

    # Язык НЕ форсим. Модель казахская, но в Алматы ~60% говорят (и матерятся)
    # по-русски — жёсткий "kk" превращал русскую речь в кашу. "auto" → воркер
    # сам определяет язык по звуку. Передать явный код можно через аргумент lang.
    language = (lang or "auto").split("-")[0].lower()

    worker_url = settings.ISSAI_WORKER_URL.rstrip("/")

    headers = {}
    if settings.ISSAI_WORKER_KEY:
        headers["X-API-Key"] = settings.ISSAI_WORKER_KEY

    try:
        # Таймаут настраивается env ISSAI_TIMEOUT (по умолчанию 300с — длинный
        # казахский разговор в очереди CPU-воркера успевает). Если воркер лежит,
        # снизь до ~60-90с: быстрее падаем в фолбэк, не держим слот обработки.
        async with httpx.AsyncClient(timeout=_ISSAI_TIMEOUT) as cli:
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

        # ── Защита от мусора ──────────────────────────────────────────────
        # Если аудио длинное (много речи), но ISSAI вернул 1-3 слова — модель
        # не справилась (чаще: русская речь через казахскую модель → каша вроде
        # «әлім кәлі»). Возвращаем "", чтобы НЕ скармливать мусор аудио-модели
        # как эталон — иначе она ставит IGNORE и теряет грубость/мат.
        audio_dur = float(data.get("audio_duration") or data.get("duration") or 0)
        words = len(text.split())
        if is_garbage(text, audio_dur):
            log.warning(
                f"ISSAI: подозрение на мусор — {words} слов на {audio_dur:.0f}с аудио "
                f"({text[:50]!r}). Отбрасываю, пусть решает аудио-модель."
            )
            return ""

        if text:
            log.info(
                f"ISSAI STT OK | lang={data.get('language', language)} "
                f"| {len(text)} симв | {words} слов | {audio_dur:.0f}с | {text[:80]!r}"
            )
            diag.update({"engine": "issai", "stage": "ok", "text": text[:160]})
        else:
            log.info("ISSAI STT: пустой ответ")
            diag.update({"engine": "issai", "stage": "empty"})
        return text

    except httpx.TimeoutException:
        log.warning(f"ISSAI STT: таймаут — воркер не ответил за {_ISSAI_TIMEOUT:.0f}с")
        diag.update({"engine": "issai", "stage": "timeout",
                     "error": f"воркер не ответил за {_ISSAI_TIMEOUT:.0f}с"})
        return ""
    except Exception as e:
        log.warning(f"ISSAI STT ошибка: {e}")
        diag.update({"engine": "issai", "stage": "connect_error",
                     "error": f"{type(e).__name__}: {str(e)[:160]}"})
        return ""
