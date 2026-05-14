# ════════════════════════════════════════════════════════════
#  Сервис: Telegram уведомления (v3.0)
#
#  Функции:
#  • send_report()         — обычный алерт при нарушении
#  • send_critical_alert() — priority=1 / fraud_risk + кнопка «Слушать»
#  • send_daily_summary()  — вечерний отчёт (22:00)
#  • send_shift_summary()  — итог смены
# ════════════════════════════════════════════════════════════

import logging
import asyncio
from datetime import datetime
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from backend.config import settings

log = logging.getLogger("notifier")
_bot: Bot | None = None


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        if not settings.TELEGRAM_BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN не задан в .env")
        _bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    return _bot


# ── Внутренние хелперы ────────────────────────────────────────────────────────

async def _send(chat_id: str, text: str, reply_markup=None):
    try:
        await get_bot().send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.error(f"Telegram ({chat_id}): {e}")


def _listen_button(audio_url: str | None) -> InlineKeyboardMarkup | None:
    """Кнопка «🎧 Слушать оригинал» — ссылка на S3-файл."""
    if not audio_url:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎧 Слушать оригинал", url=audio_url)
    ]])


# ── Публичные функции ─────────────────────────────────────────────────────────

async def send_report(
    chat_id: str,
    location_name: str,
    transcript: str,
    found: dict,
    tone: str,
    score: float,
    audio_url: str | None = None,
):
    """Обычный отчёт при нарушении (грубость / негативный тон)."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = []
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

    tone_map = {"positive": "😊 Доброжелательный", "negative": "😤 Раздражённый", "neutral": "😐 Нейтральный"}
    lines.append(f"\n🎭 Тон: {tone_map.get(tone, '😐 Нейтральный')}")
    lines.append(f"⭐ Оценка: *{score:.0f}/100*")

    markup = _listen_button(audio_url)
    await _send(chat_id, "\n".join(lines), reply_markup=markup)


async def send_critical_alert(data: dict):
    """
    Мгновенный алерт при priority=1 / CRITICAL_FRAUD_RISK.

    data: telegram_chat, location_name, summary, audio_url, sha256, transcript
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
        f"📍 *{location_name}*\n"
        f"🕐 {ts}\n\n"
        f"📋 *Суть:* {summary}\n"
    )
    if sha256:
        text += f"\n🔐 SHA256: `{sha256}...`"

    markup = _listen_button(audio_url) if audio_url else None
    await _send(chat_id, text, reply_markup=markup)


async def send_incident_alert(
    chat_id: str,
    location_name: str,
    incident_type: str,
    description: str,
    incident_id: int | None = None,
    proof_s3_url: str | None = None,
    detected_phone: str | None = None,
    tx_amount: float | None = None,
    tx_receipt_id: str | None = None,
    tx_items: list | None = None,
):
    """
    Интерактивный Telegram-алерт об инциденте.

    Формат:
      🔴 ТРЕВОГА: Подозрение на кражу
      Точка: "Кофейня на Абая"
      Продавец продиктовал номер 8707... которого нет в белом списке.

      Кнопки:
        [🎧 Прослушать запись]   — если есть proof_s3_url
        [📊 Данные чека]         — если есть tx_amount
    """
    if not chat_id:
        return

    _TYPE_LABELS = {
        "KASPI_FRAUD": "🔴 ТРЕВОГА: Подозрение на кражу",
        "FRAUD":       "🚨 КРИТИЧНО: Кассовый разрыв",
        "AGGRESSION":  "⚠️ НАРУШЕНИЕ: Грубость / конфликт",
        "UPSELL_GAP":  "📉 Допродажа не пробита",
    }
    title = _TYPE_LABELS.get(incident_type, f"⚠️ Инцидент: {incident_type}")
    ts    = datetime.now().strftime("%d.%m.%Y %H:%M")

    lines = [
        f"*{title}*",
        f"",
        f"🏪 Точка: *{location_name}*",
        f"🕐 {ts}",
        f"",
        f"📋 {description}",
    ]

    if detected_phone:
        lines.append(f"\n📱 Продиктованный номер: `{detected_phone}`")
        lines.append("_Этого номера нет в белом списке владельца_")

    # Строка 1: аудио (url-кнопка)
    row1 = []
    if proof_s3_url:
        row1.append(InlineKeyboardButton("🎧 Прослушать запись", url=proof_s3_url))

    # Строка 2: подтверждение / ошибка (callback-кнопки)
    row2 = []
    if incident_id:
        row2.append(InlineKeyboardButton("✅ Подтвердить", callback_data=f"tc_confirm:{incident_id}"))
        row2.append(InlineKeyboardButton("❌ Ошибка",      callback_data=f"tc_fp:{incident_id}"))

    # Данные чека — вставляем текстом (нельзя открыть как URL)
    if tx_amount is not None:
        lines.append(f"\n📊 *Данные чека:*")
        lines.append(f"  Сумма: `{tx_amount:,.0f} ₸`")
        if tx_receipt_id:
            lines.append(f"  Чек №: `{tx_receipt_id}`")
        if tx_items:
            lines.append("  Позиции:")
            for item in (tx_items or [])[:5]:
                name  = item.get("name", "?")
                qty   = item.get("qty") or item.get("quantity") or 1
                price = item.get("price") or item.get("sum") or "—"
                lines.append(f"    • {name} × {qty} = {price} ₸")

    all_rows = [r for r in [row1, row2] if r]
    markup = InlineKeyboardMarkup(all_rows) if all_rows else None
    await _send(chat_id, "\n".join(lines), reply_markup=markup)


async def send_daily_summary(chat_id: str, location_name: str, stats: dict):
    """
    Вечерний отчёт (отправляется в ~22:00 автоматически).

    stats: {
      total, upsell_count, upsell_pct,
      avg_satisfaction, fraud_risks,
      negative_count, greeting_pct
    }
    """
    total     = stats.get("total", 0)
    if total == 0:
        return

    upsell_pct   = stats.get("upsell_pct", 0)
    avg_sat      = stats.get("avg_satisfaction", 0.0)
    fraud_risks  = stats.get("fraud_risks", 0)
    negative     = stats.get("negative_count", 0)
    greeting_pct = stats.get("greeting_pct", 0)

    sat_stars = "⭐" * round(avg_sat) if avg_sat else "—"
    fraud_line = f"\n🚨 Подозрений на фрод: *{fraud_risks}*" if fraud_risks else ""
    neg_line   = f"\n😤 Негативных разговоров: *{negative}*" if negative else ""

    score_emoji = "🟢" if avg_sat >= 4 else "🟡" if avg_sat >= 3 else "🔴"

    text = (
        f"📊 *ИТОГ ДНЯ*\n"
        f"🏪 {location_name}  |  {datetime.now().strftime('%d.%m.%Y')}\n\n"
        f"💬 Всего разговоров: *{total}*\n"
        f"👋 Приветствия: *{greeting_pct:.0f}%*\n"
        f"🎯 Допродажи предложены: *{upsell_pct:.0f}%*\n"
        f"{score_emoji} Средняя оценка клиента: *{avg_sat:.1f}/5* {sat_stars}"
        f"{fraud_line}"
        f"{neg_line}\n\n"
        f"_Подробнее — в дашборде_"
    )
    await _send(chat_id, text)


async def send_ok_report(
    chat_id: str,
    location_name: str,
    transcript: str,
    tone: str,
    score: float,
    upsell: bool,
    greeting: bool,
):
    """Краткое сообщение для обычного разговора без нарушений."""
    tone_map = {"positive": "😊 Доброжелательный", "neutral": "😐 Нейтральный", "negative": "😤 Раздражённый"}
    upsell_str = "✅ допродажа предложена" if upsell else "—"
    greet_str  = "✅" if greeting else "❌"
    text = (
        f"✅ *{location_name}* — разговор в норме\n\n"
        f"{tone_map.get(tone, '😐 Нейтральный')} · Оценка: *{score:.0f}/100*\n"
        f"👋 Приветствие: {greet_str}  |  🎯 {upsell_str}\n\n"
        f"_{transcript[:200]}_"
    )
    await _send(chat_id, text)


async def send_shift_summary(chat_id: str, location_name: str, shift_data: dict):
    """Итог смены."""
    s     = shift_data
    total = s.get("total_conversations", 1) or 1

    def pct(val):
        return round((val or 0) / total * 100)

    score = s.get("score", 0)
    emoji = "🟢" if score >= 80 else "🟡" if score >= 60 else "🔴"

    text = (
        f"📊 *ИТОГ СМЕНЫ*\n"
        f"🏪 {location_name}\n\n"
        f"💬 Разговоров: *{total}*\n\n"
        f"✅ Приветствия:   *{s.get('greetings_count',0)}* ({pct(s.get('greetings_count',0))}%)\n"
        f"✅ Благодарности: *{s.get('thanks_count',0)}* ({pct(s.get('thanks_count',0))}%)\n"
        f"✅ Прощания:      *{s.get('goodbye_count',0)}* ({pct(s.get('goodbye_count',0))}%)\n"
        f"⭐ Допродажи:     *{s.get('bonus_count',0)}* ({pct(s.get('bonus_count',0))}%)\n\n"
        f"😊 Позитивный тон: *{s.get('positive_tone_count',0)}* раз\n"
        f"😤 Негативный тон: *{s.get('negative_tone_count',0)}* раз\n"
    )
    if s.get("bad_count", 0):
        text += f"\n⚠️ Грубость: *{s['bad_count']}* раз"
    if s.get("fraud_count", 0):
        text += f"\n🚨 Мошенничество: *{s['fraud_count']}* раз"

    text += f"\n\n{emoji} *Оценка смены: {score:.0f}/100*"
    await _send(chat_id, text)
