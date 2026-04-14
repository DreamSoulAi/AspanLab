# ════════════════════════════════════════════════════════════
#  Сервис: Транскрипция через gpt-4o-mini-transcribe
#  Автоопределение языка (русский / казахский / любой)
# ════════════════════════════════════════════════════════════

import io
import logging
from openai import AsyncOpenAI
from backend.config import settings

log = logging.getLogger("transcribe")
client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


async def transcribe(wav_bytes: bytes, language: str = None) -> str | None:
    """
    Отправляем WAV в gpt-4o-mini-transcribe, получаем текст.
    language=None → автоопределение (русский, казахский, любой)
    Возвращает None если речь не распознана.
    """
    try:
        buf = io.BytesIO(wav_bytes)
        buf.name = "audio.wav"

        params = dict(
            model="gpt-4o-mini-transcribe",
            file=buf,
        )
        # Передаём язык только если явно задан
        if language:
            params["language"] = language

        result = await client.audio.transcriptions.create(**params)

        text = result.text.strip()
        if not text or len(text) < 3:
            return None

        log.info(f"Транскрипция: {text!r}")
        return text

    except Exception as e:
        log.error(f"Ошибка транскрипции: {e}")
        return None
