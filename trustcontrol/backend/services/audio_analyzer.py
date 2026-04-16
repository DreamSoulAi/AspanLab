# ════════════════════════════════════════════════════════════
#  Сервис: Единый анализ аудио через gpt-4o-mini-audio-preview
#
#  Один API-запрос заменяет три:
#    1. Транскрипция (Whisper)
#    2. Анализ фраз и тона
#    3. GPT-резюме
#
#  Возвращает is_business + priority для фильтрации мусора и
#  триггера архивирования в S3 при priority=1.
# ════════════════════════════════════════════════════════════

import base64
import json
import logging
from openai import AsyncOpenAI
from backend.config import settings
from backend.services.gpt_analyzer import gpt_analyze  # fallback для text-only

log = logging.getLogger("audio_analyzer")
client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

_AUDIO_MODEL    = "gpt-4o-mini-audio-preview"
_FALLBACK_MODEL = "gpt-4o-mini-transcribe"

# Системный промпт: сначала фильтрует «контекстный мусор», затем анализирует.
_PROMPT = """Ты AI-аналитик качества обслуживания клиентов.
Перед тобой аудиозапись с кассы или торговой точки в Казахстане (русский или казахский язык).

━━━ ШАГ 1: ПРОВЕРКА НА МУСОР ━━━
Если запись содержит любое из следующего — это НЕ рабочий разговор:
  • Звуки из видео соцсетей (TikTok, YouTube, Instagram, ВКонтакте)
  • Музыка, пение, фоновые треки
  • Личные звонки (не связанные с обслуживанием клиента)
  • Обрывки фраз без реального диалога кассир↔клиент
  • Просто фоновый шум, тишина, звуки ремонта, транспорта
  • Менее 2 реплик (нет полноценного диалога)

Если это мусор — верни ТОЛЬКО:
{"status":"IGNORE","is_business":false,"priority":0,"transcript":"","summary":"Нерелевантная запись — не бизнес-диалог"}

━━━ ШАГ 2: АНАЛИЗ РАБОЧЕГО РАЗГОВОРА ━━━
Если это настоящий диалог кассир↔клиент — верни ТОЛЬКО валидный JSON:

{
  "status": "OK",
  "transcript": "полный текст разговора дословно",
  "language": "ru|kk|mixed",
  "is_business": true,
  "priority": <0 или 1>,
  "speakers": [
    {"role": "cashier", "text": "текст кассира"},
    {"role": "customer", "text": "текст клиента"}
  ],
  "tone": "positive|negative|neutral",
  "score": <целое число 0-100>,
  "summary": "1-2 предложения: суть разговора для дашборда",
  "events": {
    "greeting":       <true|false>,
    "farewell":       <true|false>,
    "upsell":         <true|false>,
    "rudeness":       <true|false>,
    "fraud_attempt":  <true|false>,
    "issue_resolved": <true|false>
  }
}

priority:
  0 — обычный разговор, всё в норме
  1 — подозрение на конфликт, грубость, мошенничество, нарушение стандартов

Критерии score:
  +15 — приветствие
  +15 — вежливость и уважительный тон
  +15 — вопрос клиента решён
  +10 — предложена допродажа или бонусная программа
  +10 — прощание
  −25 — грубость или пренебрежение
  −50 — попытка мошенничества (деньги мимо кассы, "по-тихому" и т.п.)
  −10 — негативный тон

СТРОГИЕ ПРАВИЛА:
- Пиши ТОЛЬКО слова которые реально были произнесены. Никаких домыслов.
- НЕ придумывай слова и фразы которых не было в записи.
- Не расшифровывай шум в слова."""


def _detect_audio_format(data: bytes) -> str:
    """Определяет формат аудио по магическим байтам."""
    if data[:4] == b"RIFF":
        return "wav"
    if data[:3] == b"ID3" or (len(data) >= 2 and data[0] == 0xFF and data[1] in (0xFB, 0xF3, 0xF2)):
        return "mp3"
    if data[:4] == b"OggS":
        return "ogg"
    return "wav"


async def analyze_audio(wav_bytes: bytes, language: str = None) -> dict:
    """
    Отправляет аудио в gpt-4o-mini-audio-preview.
    Возвращает словарь с ключами:
      status, is_business, priority, transcript, speakers,
      language, tone, score, summary, events

    status="IGNORE" означает мусор/нерабочий разговор — сохранять не нужно.
    При любой ошибке возвращает пустой dict — вызывающий код использует fallback.
    """
    if not settings.OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY не задан — аудио-анализ недоступен")
        return {}

    try:
        audio_b64    = base64.b64encode(wav_bytes).decode()
        audio_format = _detect_audio_format(wav_bytes)
        lang_hint    = f"\nЯзык записи: {language}." if language else ""

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
                                "format": audio_format,
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
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        result = json.loads(raw.strip())

        # IGNORE: мусорная запись — возвращаем как есть
        if result.get("status") == "IGNORE" or not result.get("is_business", True):
            log.info("GPT audio | IGNORE — нерабочий контент (музыка/шум/TikTok)")
            return {
                "status":      "IGNORE",
                "is_business": False,
                "priority":    0,
                "transcript":  "",
                "summary":     result.get("summary", "Нерелевантная запись"),
            }

        result["score"]    = max(0, min(100, int(result.get("score", 50))))
        result["priority"] = int(result.get("priority", 0))
        result.setdefault("status", "OK")
        result.setdefault("is_business", True)

        log.info(
            f"GPT audio | score={result['score']} "
            f"| priority={result['priority']} "
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
      Пробует gpt-4o-mini-audio-preview (транскрипция + анализ + фильтрация мусора).
      Если модель недоступна → транскрибирует через gpt-4o-mini-transcribe,
      затем анализирует текст через gpt-4o-mini.

    Режим 2 — текст (от local-whisper воркера):
      Сразу анализирует текст через gpt-4o-mini.

    Возвращает dict с полями: status, is_business, priority, transcript, ...
    Если status="IGNORE" — запись не является рабочим разговором.
    """
    # ── Режим 2: уже есть транскрипт ────────────────────────
    if transcript_text and transcript_text.strip():
        gpt = await gpt_analyze(transcript_text)
        return {
            "status":      "OK",
            "is_business": True,
            "priority":    gpt.get("priority", 0),
            "transcript":  transcript_text.strip(),
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

        # IGNORE или пустой результат — не продолжаем
        if result.get("status") == "IGNORE":
            return result

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
            "status":      "OK",
            "is_business": True,
            "priority":    gpt.get("priority", 0),
            "transcript":  text,
            "tone":        gpt.get("tone", "neutral"),
            "score":       gpt.get("score", 50),
            "summary":     gpt.get("summary", ""),
            "speakers":    [],
            "events":      {},
            "language":    language or "ru",
        }

    return {}
