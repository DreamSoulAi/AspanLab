# ════════════════════════════════════════════════════════════
#  Сервис: Единый анализ аудио — GPT-4o-mini-audio-preview
#
#  Один API-запрос за раз:
#    • Транскрипция (Whisper-level)
#    • Бизнес-аналитика (is_business, priority, payment_confirmed,
#      upsell_attempt, customer_satisfaction, is_personal_talk)
#    • Фильтрация мусора IGNORE (TikTok / музыка / шум)
# ════════════════════════════════════════════════════════════

import base64
import json
import logging
from openai import AsyncOpenAI
from backend.config import settings
from backend.services.gpt_analyzer import gpt_analyze

log = logging.getLogger("audio_analyzer")
client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

_AUDIO_MODEL    = "gpt-4o-mini-audio-preview"
_FALLBACK_MODEL = "gpt-4o-mini-transcribe"

_PROMPT = """Ты AI-аудитор качества обслуживания и финансовой безопасности бизнеса.
Перед тобой аудиозапись с торговой точки в Казахстане.

🌐 ВАЖНО — КОД-СВИТЧИНГ (смешение языков):
В Алматы и других городах Казахстана сотрудники и клиенты часто говорят смесью
русского и казахского в одном предложении. Это норма, а не ошибка.
Примеры: «Рахмет, чек нужен ба?», «Сизге сироп қосу керек пе?», «Мын теңге болады»,
«Екі кофе, пожалуйста», «Картамен төлейміз ба?».

Ты ПОНИМАЕШЬ оба языка. Обрабатывай код-свитчинг как единый диалог.
При определении payment_confirmed и upsell_attempt учитывай:
  • Казахские числа: бір=1, екі=2, үш=3, төрт=4, бес=5, алты=6,
    жеті=7, сегіз=8, тоғыз=9, он=10, жүз=100, мың=1000, екі мың=2000
  • Оплата по-казахски: «төлейміз», «карта», «қолма-қол», «сдача жоқ»
  • Допродажа по-казахски: «тағы не керек?», «қосамыз ба?», «акция бар»

━━━ ШАГ 1: ПРОВЕРКА НА МУСОР ━━━
Если запись содержит любое из следующего:
  • Звуки из видео соцсетей (TikTok, YouTube, Instagram)
  • Музыка, пение, фоновые треки
  • Личный звонок не связанный с работой — верни is_personal_talk: true
  • Обрывки фраз без реального диалога (<2 реплик)
  • Фоновый шум, тишина, ремонт, транспорт

→ Если это НЕ разговор на точке: {"status":"IGNORE","is_business":false,"is_personal_talk":false,"priority":0,"transcript":"","summary":"Нерелевантная запись"}
→ Если ЛИЧНЫЙ разговор сотрудника: {"status":"PERSONAL","is_business":false,"is_personal_talk":true,"priority":0,"transcript":"","summary":"Личный разговор сотрудника"}

━━━ ШАГ 2: АНАЛИЗ РАБОЧЕГО РАЗГОВОРА ━━━
Верни ТОЛЬКО валидный JSON без лишнего текста:

{
  "status": "OK",
  "transcript": "полный текст дословно",
  "language": "ru|kk|mixed",
  "is_business": true,
  "is_personal_talk": false,
  "priority": <0 или 1>,

  "payment_confirmed": <true|false>,
  "upsell_attempt": <true|false>,
  "customer_satisfaction": <1-5>,

  "speakers": [
    {"role": "cashier", "text": "текст кассира"},
    {"role": "customer", "text": "текст клиента"}
  ],
  "tone": "positive|negative|neutral",
  "score": <0-100>,
  "summary": "1-2 предложения: суть разговора для дашборда владельца",
  "events": {
    "greeting":       <true|false>,
    "farewell":       <true|false>,
    "upsell":         <true|false>,
    "rudeness":       <true|false>,
    "fraud_attempt":  <true|false>,
    "issue_resolved": <true|false>
  }
}

━━━ ПРАВИЛА ПОЛЕЙ ━━━
priority: 0 = норма, 1 = конфликт / подозрение на фрод / грубость

payment_confirmed = true, если слышны явные признаки завершения оплаты:
  • Названа сумма («с вас 2500», «итого пятьсот»)
  • Клиент передаёт деньги / прикладывает карту
  • Слышно подтверждение терминала или фраза «оплата прошла»

upsell_attempt = true, если сотрудник ПРЕДЛОЖИЛ доп. товар/услугу:
  • «Хотите сироп к кофе?», «Возьмёте десерт?», «У нас акция на...»
  • Предложение бонусной карты / скидки

customer_satisfaction (1-5):
  5 — доволен, благодарит, хвалит
  4 — нейтрально-доволен
  3 — нейтрально
  2 — раздражён, недоволен обслуживанием
  1 — злится, ругается, угрожает уйти

score критерии:
  +15 приветствие | +15 вежливость | +15 вопрос решён
  +10 допродажа   | +10 прощание
  −25 грубость    | −50 попытка мошенничества | −10 негативный тон

ВАЖНО: Пиши ТОЛЬКО слова которые реально были произнесены. Не придумывай."""


def _detect_audio_format(data: bytes) -> str:
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
    Возвращает полный словарь аналитики или {} при ошибке.
    """
    if not settings.OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY не задан")
        return {}

    try:
        audio_b64    = base64.b64encode(wav_bytes).decode()
        audio_format = _detect_audio_format(wav_bytes)
        lang_hint    = f"\nЯзык записи: {language}." if language else ""

        response = await client.chat.completions.create(
            model=_AUDIO_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": audio_b64, "format": audio_format},
                    },
                    {"type": "text", "text": _PROMPT + lang_hint},
                ],
            }],
            max_tokens=800,
            temperature=0.1,
        )

        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        result = json.loads(raw.strip())
        status = result.get("status", "OK")

        if status in ("IGNORE", "PERSONAL") or not result.get("is_business", True):
            log.info(f"GPT audio | {status} — нерабочий контент")
            return {
                "status":          status,
                "is_business":     False,
                "is_personal_talk": result.get("is_personal_talk", False),
                "priority":        0,
                "transcript":      "",
                "summary":         result.get("summary", ""),
            }

        result["score"]    = max(0, min(100, int(result.get("score", 50))))
        result["priority"] = int(result.get("priority", 0))
        result["customer_satisfaction"] = max(1, min(5, int(result.get("customer_satisfaction", 3))))
        result.setdefault("status", "OK")
        result.setdefault("is_business", True)
        result.setdefault("is_personal_talk", False)
        result.setdefault("payment_confirmed", None)
        result.setdefault("upsell_attempt", None)

        log.info(
            f"GPT audio | score={result['score']} "
            f"| priority={result['priority']} "
            f"| sat={result['customer_satisfaction']} "
            f"| payment={result['payment_confirmed']} "
            f"| upsell={result['upsell_attempt']}"
        )
        return result

    except Exception as e:
        log.warning(f"gpt-4o-mini-audio-preview ошибка: {e}")
        return {}


async def analyze_audio_with_fallback(
    wav_bytes: bytes | None,
    transcript_text: str | None,
    language: str = None,
) -> dict:
    """
    Универсальная точка входа.

    Режим 1 — аудио: gpt-4o-mini-audio-preview → fallback transcribe + gpt-4o-mini.
    Режим 2 — текст: сразу gpt-4o-mini (local-whisper режим).

    status="IGNORE"   — мусор, не сохранять
    status="PERSONAL" — личный разговор, сохранить как is_hidden=true
    status="OK"       — рабочий разговор, анализировать полностью
    """
    if transcript_text and transcript_text.strip():
        gpt = await gpt_analyze(transcript_text)
        return {
            "status":               "OK",
            "is_business":          True,
            "is_personal_talk":     False,
            "priority":             gpt.get("priority", 0),
            "transcript":           transcript_text.strip(),
            "tone":                 gpt.get("tone", "neutral"),
            "score":                gpt.get("score", 50),
            "summary":              gpt.get("summary", ""),
            "speakers":             [],
            "events":               {},
            "language":             language or "ru",
            "payment_confirmed":    None,
            "upsell_attempt":       None,
            "customer_satisfaction": 3,
        }

    if wav_bytes:
        result = await analyze_audio(wav_bytes, language)

        if result.get("status") in ("IGNORE", "PERSONAL"):
            return result

        if result and result.get("transcript"):
            return result

        # Fallback: транскрипция отдельно → text анализ
        log.info("Fallback: gpt-4o-mini-transcribe + text analysis")
        try:
            import io as _io
            buf = _io.BytesIO(wav_bytes)
            buf.name = "audio.wav"
            tr = await client.audio.transcriptions.create(
                model=_FALLBACK_MODEL, file=buf, language=language,
            )
            text = tr.text.strip()
        except Exception as e:
            log.error(f"Fallback транскрипция не удалась: {e}")
            return {}

        if not text or len(text) < 3:
            return {}

        gpt = await gpt_analyze(text)
        return {
            "status":               "OK",
            "is_business":          True,
            "is_personal_talk":     False,
            "priority":             gpt.get("priority", 0),
            "transcript":           text,
            "tone":                 gpt.get("tone", "neutral"),
            "score":                gpt.get("score", 50),
            "summary":              gpt.get("summary", ""),
            "speakers":             [],
            "events":               {},
            "language":             language or "ru",
            "payment_confirmed":    None,
            "upsell_attempt":       None,
            "customer_satisfaction": 3,
        }

    return {}
