# ════════════════════════════════════════════════════════════
#  Сервис: Единый анализ аудио через gpt-4o-mini-audio-preview
#
#  Один API-запрос заменяет три:
#    1. Транскрипция (Whisper)
#    2. Анализ фраз и тона
#    3. GPT-резюме
#
#  Также возвращает диаризацию (кто говорит: кассир или клиент).
# ════════════════════════════════════════════════════════════

import base64
import json
import logging
from openai import AsyncOpenAI
from backend.config import settings
from backend.services.gpt_analyzer import gpt_analyze  # fallback для text-only

log = logging.getLogger("audio_analyzer")
client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

# Модель для аудио-анализа. При недоступности — fallback на transcribe
_AUDIO_MODEL    = "gpt-4o-mini-audio-preview"
_FALLBACK_MODEL = "gpt-4o-mini-transcribe"

_PROMPT = """Ты AI-аналитик качества обслуживания клиентов.
Перед тобой аудиозапись разговора на кассе/в кафе в Казахстане (русский или казахский язык).

Проанализируй запись и верни ТОЛЬКО валидный JSON без лишнего текста:

{
  "transcript": "полный текст разговора дословно",
  "language": "ru|kk|mixed",
  "speakers": [
    {"role": "cashier", "text": "текст кассира"},
    {"role": "customer", "text": "текст клиента"}
  ],
  "tone": "positive|negative|neutral",
  "score": <целое число 0-100>,
  "summary": "1-2 предложения: был ли решён вопрос клиента",
  "events": {
    "greeting":       <true|false>,
    "farewell":       <true|false>,
    "upsell":         <true|false>,
    "rudeness":       <true|false>,
    "fraud_attempt":  <true|false>,
    "issue_resolved": <true|false>
  }
}

Критерии score:
  +15 — приветствие
  +15 — вежливость и уважительный тон
  +15 — вопрос клиента решён
  +10 — предложена допродажа или бонусная программа
  +10 — прощание
  −25 — грубость или пренебрежение
  −50 — попытка мошенничества (деньги мимо кассы, "по-тихому" и т.п.)
  −10 — негативный тон

Если в записи нет речи или она неразборчива:
{"transcript":"","tone":"neutral","score":50,"summary":"Тишина или нет речи","speakers":[],"events":{},"language":"ru"}"""


async def analyze_audio(wav_bytes: bytes, language: str = None) -> dict:
    """
    Отправляет WAV в gpt-4o-mini-audio-preview.
    Возвращает словарь с ключами:
      transcript, speakers, language, tone, score, summary, events

    При любой ошибке возвращает пустой dict — вызывающий код
    должен использовать fallback (транскрипт + regex-анализатор).
    """
    if not settings.OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY не задан — аудио-анализ недоступен")
        return {}

    try:
        audio_b64 = base64.b64encode(wav_bytes).decode()
        lang_hint = f"\nЯзык записи: {language}." if language else ""

        response = await client.chat.completions.create(
            model=_AUDIO_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data":   audio_b64,
                                "format": "wav",
                            },
                        },
                        {
                            "type": "text",
                            "text": _PROMPT + lang_hint,
                        },
                    ],
                }
            ],
            max_tokens=700,
            temperature=0.1,
        )

        raw = response.choices[0].message.content.strip()
        # Убираем markdown-обёртку если модель её добавила
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        result = json.loads(raw.strip())

        # Нормализуем score
        result["score"] = max(0, min(100, int(result.get("score", 50))))

        log.info(
            f"GPT audio | score={result['score']} "
            f"| tone={result.get('tone')} "
            f"| {result.get('summary','')[:60]}"
        )
        return result

    except Exception as e:
        log.warning(f"gpt-4o-mini-audio-preview недоступен: {e}")
        return {}


async def analyze_audio_with_fallback(
    wav_bytes: bytes | None,
    transcript_text: str | None,
    language: str = None,
) -> dict:
    """
    Универсальная точка входа для анализа.

    Режим 1 — аудио:
      Пробует gpt-4o-mini-audio-preview (транскрипция + анализ за один вызов).
      Если модель недоступна → транскрибирует через gpt-4o-mini-transcribe,
      затем анализирует текст через gpt-4o-mini.

    Режим 2 — текст (от local-whisper воркера):
      Сразу анализирует текст через gpt-4o-mini.
    """
    # ── Режим 2: уже есть транскрипт ────────────────────────
    if transcript_text and transcript_text.strip():
        gpt = await gpt_analyze(transcript_text)
        return {
            "transcript": transcript_text.strip(),
            "tone":        gpt.get("tone", "neutral"),
            "score":       gpt.get("score", 50),
            "summary":     gpt.get("summary", ""),
            "speakers":    [],
            "events":      {
                "greeting":       False,
                "farewell":       False,
                "upsell":         False,
                "rudeness":       False,
                "fraud_attempt":  False,
                "issue_resolved": False,
            },
            "language": language or "ru",
        }

    # ── Режим 1: аудио → gpt-4o-mini-audio-preview ──────────
    if wav_bytes:
        result = await analyze_audio(wav_bytes, language)
        if result and result.get("transcript"):
            return result

        # Fallback: транскрипция через gpt-4o-mini-transcribe
        log.info("Fallback: gpt-4o-mini-transcribe + text analysis")
        try:
            import io as _io
            buf = _io.BytesIO(wav_bytes)
            buf.name = "audio.wav"
            tr = await client.audio.transcriptions.create(
                model=_FALLBACK_MODEL,
                file=buf,
                language=language,
            )
            text = tr.text.strip()
        except Exception as e:
            log.error(f"Транскрипция (fallback) не удалась: {e}")
            return {}

        if not text or len(text) < 3:
            return {}

        gpt = await gpt_analyze(text)
        return {
            "transcript": text,
            "tone":        gpt.get("tone", "neutral"),
            "score":       gpt.get("score", 50),
            "summary":     gpt.get("summary", ""),
            "speakers":    [],
            "events":      {},
            "language":    language or "ru",
        }

    return {}
