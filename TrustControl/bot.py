#!/usr/bin/env python3
"""
TrustControl — Telegram-бот для управления и просмотра статистики
Команды: /start /stats /alerts /status
"""

import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from config import TELEGRAM_BOT_TOKEN, LOCATION_NAME
from database import Database

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

db = Database()

# ── Главное меню ──────────────────────────────────────────────

MAIN_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("📊 Статистика за 7 дней",  callback_data="stats_7")],
    [InlineKeyboardButton("📈 Статистика за 30 дней", callback_data="stats_30")],
    [InlineKeyboardButton("🚨 Последние тревоги",     callback_data="alerts")],
    [InlineKeyboardButton("ℹ️ Статус системы",        callback_data="status")],
])


# ── Команды ───────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🏪 *TrustControl — {LOCATION_NAME}*\n\nВыберите действие:",
        reply_markup=MAIN_KEYBOARD,
        parse_mode="Markdown",
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    days = int(context.args[0]) if context.args else 7
    await update.message.reply_text(
        _format_stats(db.get_stats(days)), parse_mode="Markdown"
    )


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_alerts(update.message)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_status_text(), parse_mode="Markdown")


# ── Обработчик кнопок ─────────────────────────────────────────

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("stats_"):
        days = int(query.data.split("_")[1])
        await query.message.reply_text(
            _format_stats(db.get_stats(days)), parse_mode="Markdown"
        )
    elif query.data == "alerts":
        await _send_alerts(query.message)
    elif query.data == "status":
        await query.message.reply_text(_status_text(), parse_mode="Markdown")


# ── Вспомогательные функции ───────────────────────────────────

def _format_stats(s: dict) -> str:
    lines = [
        f"📊 *Статистика за {s['period_days']} дней*",
        f"📍 {LOCATION_NAME}",
        "",
        f"💬 Всего разговоров: *{s['total']}*",
        f"✅ Приветствия:       *{s['with_greeting']}* ({s['greeting_rate']}%)",
        f"🙏 Благодарности:     *{s['with_thanks']}* ({s['thanks_rate']}%)",
        f"⭐ Допродажи:         *{s['with_upsell']}* ({s['upsell_rate']}%)",
        "",
        f"⚠️ Грубость:          *{s['rudeness_alerts']}* случаев",
        f"🚨 Мошенничество:     *{s['fraud_alerts']}* случаев",
    ]
    if s["tones"]:
        lines.append("\n🎭 *Тон сотрудников:*")
        for tone, cnt in s["tones"]:
            lines.append(f"  {tone}: {cnt}")
    return "\n".join(lines)


async def _send_alerts(message):
    rows = db.get_recent_alerts(limit=10)
    if not rows:
        await message.reply_text("✅ Тревог не зафиксировано")
        return

    lines = ["🚨 *Последние тревоги:*\n"]
    for row in rows:
        preview = (row["transcript"] or "")[:80]
        lines.append(f"• {row['timestamp']} — *{row['alert_type']}*")
        lines.append(f"  _{preview}..._\n")

    await message.reply_text("\n".join(lines), parse_mode="Markdown")


def _status_text() -> str:
    return (
        f"✅ *Система работает*\n\n"
        f"📍 Точка: {LOCATION_NAME}\n"
        f"🕐 Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


# ── Запуск ───────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_button))

    logger.info("Бот запущен...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
