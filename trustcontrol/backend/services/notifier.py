# ════════════════════════════════════════════════════════════
#  Сервис: Telegram уведомления
# ════════════════════════════════════════════════════════════

import logging
import asyncio
from datetime import datetime
from telegram import Bot
from telegram.constants import ParseMode
from backend.config import settings

log = logging.getLogger("notifier")
_bot = None


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        if not settings.TELEGRAM_BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN не задан в .env")
        _bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    return _bot


def _build_report(
    location_name: str,
    transcript: str,
    found: dict,
    tone: str,
    score: float,
    ts: str,
) -> str:
    lines = []

    # Заголовок тревоги
    if "🚨 МОШЕННИЧЕСТВО" in found:
        lines += ["🚨🚨🚨 *СРОЧНО! ЛЕВАК НА КАССЕ!* 🚨🚨🚨", ""]
    elif "⚠️ Грубость" in found:
        lines += ["🔴 *НАРУШЕНИЕ: ГРУБОСТЬ НА КАССЕ*", ""]

    lines += [
        f"🏪 *{location_name}*  |  `{ts}`",
        "",
        f"📝 *Разговор:*",
        f"_{transcript[:500]}_",
        "",
    ]

    if found:
        lines.append("🔍 *Обнаружено:*")
        for cat, hits in found.items():
            lines.append(f"  {cat}: {', '.join(f'`{h}`' for h in hits[:3])}")

    # Тон
    tone_map = {
        "positive": "😊 Доброжелательный",
        "negative": "😤 Раздражённый",
        "neutral":  "😐 Нейтральный",
    }
    lines.append(f"\n🎭 Тон: {tone_map.get(tone, '😐 Нейтральный')}")
    lines.append(f"⭐ Оценка: *{score:.0f}/100*")

    return "\n".join(lines)


async def _send(chat_id: str, text: str):
    """Отправляем сообщение в Telegram."""
    try:
        await get_bot().send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        log.error(f"Ошибка Telegram ({chat_id}): {e}")


async def send_report(
    chat_id: str,
    location_name: str,
    transcript: str,
    found: dict,
    tone: str,
    score: float,
):
    """Отправляем обычный отчёт."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Срочный алерт при мошенничестве
    if "🚨 МОШЕННИЧЕСТВО" in found:
        await _send(chat_id,
            f"🚨🚨🚨 *СРОЧНО! ЛЕВАК!*\n\n"
            f"📍 *{location_name}*\n"
            f"🕐 {ts}\n\n"
            f"Немедленно проверьте кассу!"
        )

    report = _build_report(location_name, transcript, found, tone, score, ts)
    await _send(chat_id, report)


async def send_critical_alert(data: dict):
    """
    Отправляет критическое уведомление когда GPT устанавливает priority=1.

    Ожидаемые поля data:
      telegram_chat  — куда слать
      location_name  — название точки
      summary        — краткая суть от GPT
      audio_url      — ссылка на запись в S3 (если есть)
      sha256         — хеш файла (первые 16 символов)
      transcript     — текст разговора

    TODO: добавить Telegram-кнопки, email, webhook в будущей версии.
    """
    chat_id = data.get("telegram_chat")
    if not chat_id:
        log.warning("send_critical_alert: telegram_chat не задан")
        return

    summary       = data.get("summary", "—")
    audio_url     = data.get("audio_url") or ""
    sha256        = (data.get("sha256") or "")[:16]
    location_name = data.get("location_name", "—")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    text = (
        f"🚨 *ПРИОРИТЕТ 1 — ТРЕБУЕТСЯ ПРОВЕРКА*\n\n"
        f"🏪 *{location_name}*\n"
        f"🕐 {ts}\n\n"
        f"📋 *Суть:* {summary}\n"
    )
    if audio_url:
        text += f"\n🔗 [Слушать запись]({audio_url})"
    if sha256:
        text += f"\n🔐 SHA256: `{sha256}...`"

    await _send(chat_id, text)


async def send_shift_summary(
    chat_id: str,
    location_name: str,
    shift_data: dict,
):
    """Итог смены."""
    s = shift_data
    total = s.get("total_conversations", 1) or 1

    def pct(val):
        return round((val or 0) / total * 100)

    score = s.get("score", 0)
    emoji = "🟢" if score >= 80 else "🟡" if score >= 60 else "🔴"

    text = (
        f"📊 *ИТОГ СМЕНЫ*\n"
        f"🏪 {location_name}\n\n"
        f"💬 Разговоров: *{total}*\n\n"
        f"✅ Приветствия:  *{s.get('greetings_count',0)}* ({pct(s.get('greetings_count',0))}%)\n"
        f"✅ Благодарности: *{s.get('thanks_count',0)}* ({pct(s.get('thanks_count',0))}%)\n"
        f"✅ Прощания:     *{s.get('goodbye_count',0)}* ({pct(s.get('goodbye_count',0))}%)\n"
        f"⭐ Допродажи:    *{s.get('bonus_count',0)}* ({pct(s.get('bonus_count',0))}%)\n\n"
        f"😊 Позитивный тон: *{s.get('positive_tone_count',0)}* раз\n"
        f"😤 Негативный тон: *{s.get('negative_tone_count',0)}* раз\n"
    )

    if s.get("bad_count", 0):
        text += f"\n⚠️ Грубость: *{s['bad_count']}* раз"
    if s.get("fraud_count", 0):
        text += f"\n🚨 Мошенничество: *{s['fraud_count']}* раз"

    text += f"\n\n{emoji} *Оценка смены: {score:.0f}/100*"

    await _send(chat_id, text)
