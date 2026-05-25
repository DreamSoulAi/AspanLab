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

🌐 ЯЗЫКИ — ЛЮБОЙ ЯЗЫК ЯВЛЯЕТСЯ НОРМОЙ:
Клиентами могут быть туристы и люди из любой страны мира. Разговор может вестись на:
  • Русском, казахском или их смеси (местная норма)
  • Английском, турецком, китайском, арабском, немецком, корейском и любом другом языке
  • Смеси любых языков — кассир может говорить на одном, клиент на другом

Ты понимаешь ВСЕ языки. Разговор с туристом — это полноценный диалог с клиентом.
Не важно на каком языке — если человек пришёл купить товар или услугу, это рабочий разговор.

Примеры туристических диалогов (всё это is_business: true):
  • «One coffee please» — «Сейчас сделаю, card or cash?»
  • «Bir kahve lütfen» — «Tamam, ne kadar?»
  • «一杯咖啡» — «Окей, карта?»
  • «Wie viel kostet das?» — «Two thousand tenge»

При определении payment_confirmed и upsell_attempt учитывай любой язык:
  • Оплата: «card», «cash», «payment», «карта», «картамен», «төлейміз»
  • Числа на казахском: бір=1, екі=2, үш=3, он=10, жүз=100, мың=1000
  • Допродажа: «anything else?», «would you like», «тағы не керек?», «қосамыз ба?»

━━━ ШАГ 1: ПРОВЕРКА НА МУСОР ━━━
Если запись содержит ТОЛЬКО любое из следующего (без живого диалога):
  • Звуки из видео соцсетей (TikTok, YouTube, Instagram, ВКонтакте)
  • Видео / сериал / фильм / новости на фоне без живого разговора
  • Музыка, пение, фоновые треки
  • Личный звонок не связанный с работой — верни is_personal_talk: true
  • Обрывки фраз без реального диалога (<2 реплик)
  • Фоновый шум, тишина, ремонт, транспорт
  • Проверка микрофона: повторяющиеся «раз», «алло», «тест», «один два три», счёт до 10
  • Нет НИКАКОГО взаимодействия между людьми (нет вопроса и ответа, нет обмена)
  • Бессмысленный набор слов без коммерческого контекста на ЛЮБОМ языке

ВАЖНО: незнакомый язык — НЕ причина для IGNORE. Туристический диалог на английском,
китайском или турецком — это такой же рабочий разговор как на русском.

→ Если это НЕ разговор на точке: {"status":"IGNORE","is_business":false,"is_personal_talk":false,"priority":0,"transcript":"","summary":"Нерелевантная запись"}
→ Если ЛИЧНЫЙ разговор сотрудника: {"status":"PERSONAL","is_business":false,"is_personal_talk":true,"priority":0,"transcript":"","summary":"Личный разговор сотрудника"}

━━━ КРИТИЧЕСКИ ВАЖНО: ФОНОВЫЕ МЕДИА И МАТ ━━━
Если в записи есть ЖИВОЙ разговор с клиентом + фоновые звуки (ТВ/видео/телефон другого человека):
  → Транскрибируй ТОЛЬКО слова живых людей (кассир и клиент).
  → Слова из фонового видео/ТВ/динамика телефона — НЕ включай в транскрипт.

Прежде чем ставить events.rudeness = true — ВСЕГДА определи источник мата:
  A. Мат произнёс сотрудник или клиент В ЖИВОМ разговоре → rudeness: true
  B. Мат слышен из ТВ / видео / телефонного динамика другого человека / фона → rudeness: false

Признаки фонового звука (не живой человек):
  • Голос слышен как через динамик (другое качество, компрессия, эхо устройства)
  • Речь не адресована кассиру/клиенту, нет ответных реплик
  • Типичный паттерн медиа: ведущий говорит без пауз для ответа, шумы экшн-сцены
  • Звонок со стороны — другой человек разговаривает по телефону рядом

Если сомневаешься (непонятно кто сказал мат) → rudeness: false, priority: 0.
Принцип: лучше пропустить сомнительное, чем ложно обвинить сотрудника.

━━━ ШАГ 2: АНАЛИЗ РАБОЧЕГО РАЗГОВОРА ━━━
Верни ТОЛЬКО валидный JSON без лишнего текста:

{
  "status": "OK",
  "transcript": "полный текст дословно на оригинальном языке",
  "language": "ru|kk|en|zh|tr|ar|de|ko|...",
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
  "summary": "1-2 предложения на РУССКОМ языке: суть разговора для дашборда владельца (даже если разговор был на другом языке)",
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

━━━ КОНЦЕПЦИИ — ОПРЕДЕЛЯЙ СУТЬ, НЕ СЛОВА ━━━
Работает на ЛЮБОМ языке. Ищи СМЫСЛ действия, а не конкретные фразы.

payment_confirmed = true — оплата реально завершилась:
  • Названа итоговая сумма И клиент платит (картой, наличными, телефоном)
  • Слышно подтверждение терминала или «оплата прошла» / «payment done»
  • НЕ считается: просто упомянули цену без факта оплаты

upsell_attempt = true — кассир САМИ предложил что-то дополнительное:
  • Клиент не просил — кассир инициировал предложение доп. товара/услуги
  • Например: «Хотите ещё что-то?», «Возьмёте десерт?», «У нас акция», «Anything else?»
  • НЕ считается: клиент сам спросил, или просто упомянули продукт в ответ

events.fraud_attempt = true — попытка увести деньги мимо кассы:
  • Кассир просит оплатить на личный номер/карту/кошелёк, минуя официальную кассу
  • «Переведи мне», «без чека», «наличкой напрямую», «pay me directly», «WeChat me»
  • Любое предложение «по-тихому» или «между нами»
  • Работает на любом языке — detect the INTENT to redirect payment

events.rudeness = true — грубость или пренебрежение к клиенту:
  • Сотрудник говорит агрессивно, презрительно, с раздражением
  • Отказывает помочь без причины, игнорирует, отчитывает клиента
  • Определяй по ТОНУ и СМЫСЛУ, не по наличию матерных слов
  • НЕ считается: мат в фоне (ТВ/телефон), личный разговор не адресованный клиенту

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
            max_tokens=2000,
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
        if not gpt.get("is_business", True):
            return {"status": "IGNORE", "is_business": False, "priority": 0, "transcript": "", "summary": gpt.get("summary", "")}
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
        if not gpt.get("is_business", True):
            return {"status": "IGNORE", "is_business": False, "priority": 0, "transcript": "", "summary": gpt.get("summary", "")}
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
