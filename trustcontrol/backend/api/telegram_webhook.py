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
#  Настройка webhook:
#    curl -F "url=https://ВАШ_ДОМЕН/telegram/webhook" \
#         https://api.telegram.org/bot{TOKEN}/setWebhook
# ════════════════════════════════════════════════════════════

import logging
from datetime import datetime

from fastapi import APIRouter, Request
from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.models.incident import Incident
from backend.models.location import Location
from backend.services.notifier import get_bot

log    = logging.getLogger("tg_webhook")
router = APIRouter()


@router.post("/webhook")
async def telegram_webhook(request: Request):
    """
    Принимает Telegram Bot API updates.
    Обрабатывает callback_query от InlineKeyboard под инцидент-алертами.
    """
    try:
        update = await request.json()
    except Exception:
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
            show_alert  = True   # всплывающее сообщение о добавлении в whitelist

    except Exception as e:
        log.error(f"Ошибка обработки callback '{data}': {e}")
        answer_text = "Ошибка обработки"
        show_alert  = True

    # Отвечаем Telegram чтобы убрать «часики» на кнопке
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
    """Подтверждает инцидент как реальное нарушение."""
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
    """
    Помечает инцидент как ошибку.
    Если KASPI_FRAUD — добавляет номер в allowed_phones (обучение системы).
    """
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

        elif incident.incident_type == "UPSELL_GAP" and incident.upsell_phrase:
            # TODO: можно добавить фразу в exclusion list
            pass

        await db.commit()

    return f"❌ Помечено как ошибка системы.{whitelisted}"
