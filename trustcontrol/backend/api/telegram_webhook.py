# ════════════════════════════════════════════════════════════
#  API: Telegram Webhook — обработка кнопок под алертами
#
#  POST /telegram/webhook   — принимает updates от Telegram
#
#  Callback data формат:
#    tc_confirm:{incident_id}  — подтвердить нарушение
#    tc_fp:{incident_id}       — ошибка (false positive)
#                                → авто-добавляет номер/фразу в whitelist
#
#  SECURITY: Webhook защищён HMAC-подписью.
#  Настройка webhook с секретом:
#    curl -F "url=https://ВАШ_ДОМЕН/telegram/webhook" \
#         -F "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
#         https://api.telegram.org/bot{TOKEN}/setWebhook
# ════════════════════════════════════════════════════════════

import hmac
import logging
import os
from datetime import datetime

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from telegram import InlineKeyboardMarkup, InlineKeyboardButton

from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.models.incident import Incident
from backend.models.location import Location
from backend.models.user import User
from backend.services.notifier import get_bot

log    = logging.getLogger("tg_webhook")
router = APIRouter()

_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")


@router.post("/webhook")
async def telegram_webhook(request: Request):
    """
    Принимает Telegram Bot API updates.
    Проверяет X-Telegram-Bot-Api-Secret-Token если TELEGRAM_WEBHOOK_SECRET задан.
    """
    if _WEBHOOK_SECRET:
        incoming = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(incoming, _WEBHOOK_SECRET):
            log.warning("Telegram webhook: неверный secret token — запрос отклонён")
            return Response(status_code=403)

    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    # Handle text messages (/start, /help, /start link_TOKEN)
    message = update.get("message", {})
    if message:
        text    = (message.get("text") or "").strip()
        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id:
            if text.startswith("/start link_"):
                token = text[len("/start link_"):]
                await _handle_tg_link(token, chat_id, message)
            elif text in ("/start", "/help", "start", "помощь"):
                await _handle_start(chat_id)
            return {"ok": True}

    callback = update.get("callback_query")
    if not callback:
        return {"ok": True}

    callback_id = callback.get("id", "")
    data        = callback.get("data", "")

    answer_text = ""
    show_alert  = False

    try:
        if data.startswith("tc_confirm:"):
            incident_id = int(data.split(":", 1)[1])
            answer_text = await _handle_confirm(incident_id)
            show_alert  = False

        elif data.startswith("tc_fp:"):
            incident_id = int(data.split(":", 1)[1])
            answer_text = await _handle_false_positive(incident_id)
            show_alert  = True

    except (ValueError, IndexError):
        return {"ok": True}
    except Exception as e:
        log.error(f"Ошибка обработки callback '{data}': {e}")
        answer_text = "Ошибка обработки"
        show_alert  = True

    try:
        bot = get_bot()
        await bot.answer_callback_query(
            callback_query_id=callback_id,
            text=answer_text,
            show_alert=show_alert,
        )
    except Exception as e:
        log.error(f"answer_callback_query ошибка: {e}")

    return {"ok": True}


async def _handle_confirm(incident_id: int) -> str:
    async with AsyncSessionLocal() as db:
        incident = await db.get(Incident, incident_id)
        if not incident:
            return "Инцидент не найден"
        if incident.status in ("resolved", "false_positive"):
            return f"Уже закрыт: {incident.status}"
        incident.status = "confirmed"
        await db.commit()
    log.info(f"Incident #{incident_id} подтверждён как нарушение")
    return "✅ Нарушение подтверждено и зафиксировано"


async def _handle_false_positive(incident_id: int) -> str:
    async with AsyncSessionLocal() as db:
        incident = await db.get(Incident, incident_id)
        if not incident:
            return "Инцидент не найден"
        if incident.status in ("resolved", "false_positive"):
            return f"Уже закрыт: {incident.status}"

        incident.status      = "false_positive"
        incident.resolved_at = datetime.utcnow()

        whitelisted = ""
        if incident.incident_type == "KASPI_FRAUD" and incident.detected_phone:
            loc = await db.get(Location, incident.location_id)
            if loc:
                phones = list(loc.allowed_phones or [])
                if incident.detected_phone not in phones:
                    phones.append(incident.detected_phone)
                    loc.allowed_phones = phones
                    whitelisted = f"\n📱 {incident.detected_phone} добавлен в белый список"
                    log.info(
                        f"[loc={loc.id}] {incident.detected_phone} "
                        f"добавлен в allowed_phones через Telegram"
                    )

        await db.commit()

    return f"❌ Помечено как ошибка системы.{whitelisted}"


async def _handle_start(chat_id: str):
    """Welcome message for /start command."""
    text = (
        "👋 *Добро пожаловать в TrustControl!*\n\n"
        "Этот бот отправляет вам:\n"
        "• 🚨 Мгновенные тревоги — грубость, мошенничество\n"
        "• 📊 Итог каждой смены\n"
        "• 📈 Ежедневный отчёт в 22:00\n\n"
        "Чтобы подключить бот к вашему аккаунту:\n"
        "1️⃣ Войдите в личный кабинет\n"
        "2️⃣ Настройки → кнопка *«Подключить через Telegram»*\n\n"
        "Всё — тревоги начнут приходить сюда автоматически."
    )
    buttons = []
    if settings.APP_URL:
        buttons.append([InlineKeyboardButton("🖥 Открыть личный кабинет", url=settings.APP_URL)])
    markup = InlineKeyboardMarkup(buttons) if buttons else None
    try:
        bot = get_bot()
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.error(f"_handle_start error: {e}")


async def _handle_tg_link(token: str, chat_id: str, message: dict):
    """Link a Telegram chat to a user account via one-time token."""
    import time
    from backend.api.auth import _tg_link_tokens

    entry = _tg_link_tokens.pop(token, None)
    if not entry:
        try:
            bot = get_bot()
            await bot.send_message(chat_id=chat_id,
                text="❌ Ссылка устарела или уже использована. Создайте новую в личном кабинете.")
        except Exception as e:
            log.error(f"tg_link reply error: {e}")
        return

    user_id, expires = entry
    if time.time() > expires:
        try:
            bot = get_bot()
            await bot.send_message(chat_id=chat_id,
                text="❌ Ссылка устарела (10 минут). Создайте новую в личном кабинете.")
        except Exception as e:
            log.error(f"tg_link expired reply error: {e}")
        return

    telegram_id = str(message.get("from", {}).get("id", chat_id))

    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if not user:
            return
        user.telegram_chat = chat_id
        user.telegram_id   = telegram_id
        await db.commit()

    log.info(f"User #{user_id} linked Telegram chat {chat_id}")
    try:
        bot = get_bot()
        await bot.send_message(chat_id=chat_id,
            text=f"✅ Telegram подключён к TrustControl!\n\nТревоги и отчёты по вашим точкам будут приходить сюда.")
    except Exception as e:
        log.error(f"tg_link success reply error: {e}")
