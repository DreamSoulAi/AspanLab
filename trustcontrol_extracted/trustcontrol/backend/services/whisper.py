# ════════════════════════════════════════════════════════════
#  Сервис: Транскрипция через OpenAI Whisper
# ════════════════════════════════════════════════════════════

import io
import logging
from openai import AsyncOpenAI
from backend.config import settings

log = logging.getLogger("whisper")
client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


async def transcribe(wav_bytes: bytes, language: str = "ru") -> str | None:
    """
    Отправляем WAV в Whisper, получаем текст.
    Возвращает None если речь не распознана.
    """
    try:
        buf = io.BytesIO(wav_bytes)
        buf.name = "audio.wav"

        result = await client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=buf,
            language=language,
        )

        text = result.text.strip()
        if not text or len(text) < 3:
            return None

        log.info(f"Транскрипция: {text!r}")
        return text

    except Exception as e:
        log.error(f"Ошибка Whisper: {e}")
        return None
