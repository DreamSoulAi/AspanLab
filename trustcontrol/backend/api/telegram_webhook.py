# ════════════════════════════════════════════════════════════
#  API: Telegram Webhook — бот с меню + обработка кнопок
#
#  POST /telegram/webhook  — принимает updates от Telegram
#
#  Команды / кнопки бота:
#    /start [token]         — приветствие или привязка аккаунта
#    📊 Отчёт за сегодня   — статистика всех точек за день
#    📍 Мои точки           — список точек пользователя
#    🚨 Тревоги             — открытые инциденты
#    👤 Профиль             — информация об аккаунте
#    ❓ Помощь              — инструкции
#
#  Callback data формат:
#    tc_confirm:{id}   — подтвердить инцидент
#    tc_fp:{id}        — ошибка (false positive)
#    tc_today          — отчёт за сегодня
#    tc_profile        — профиль
#    tc_locations      — точки
#    tc_alerts         — тревоги
#    tc_help           — помощь
#
#  Привязка Telegram:
#    POST /api/auth/tg-link   → возвращает token
#    Пользователь кликает ссылку t.me/BOT?start=token
#    Бот получает /start token и линкует chat_id к аккаунту
#
#  SECURITY: Webhook защищён HMAC-подписью.
# ════════════════════════════════════════════════════════════

import hashlib
import hmac
import logging
import os
import time
from datetime import datetime

from fastapi import APIRouter, Request, Response
from sqlalchemy import select, update as sa_update
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from telegram.constants import ParseMode

from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.models.incident import Incident
from backend.models.location import Location
from backend.models.otp_code import OtpCode
from backend.models.report   import Report
from backend.models.user     import User
from backend.services.notifier import get_bot

log    = logging.getLogger("tg_webhook")
router = APIRouter()

_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

# ── Account-linking tokens (HMAC-signed, no server-side storage) ──────────────
# Token format: {user_id}_{timestamp}_{hmac_hex32}
# All chars are hex digits + underscore → valid Telegram start parameter.
# TTL is 10 minutes, verified via timestamp in the token itself.

def generate_link_token(payload: dict) -> str:
    """
    Create a signed link token for Telegram deep-link (/start TOKEN).
    Survives server restarts — no in-memory or DB storage needed.
    payload must contain 'user_id' (int).
    """
    user_id = int(payload.get("user_id", 0))
    ts  = int(time.time())
    msg = f"{user_id}:{ts}"
    sig = hmac.new(settings.SECRET_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{user_id}_{ts}_{sig}"


def _verify_link_token(token: str) -> dict | None:
    """Verify a link token. Returns {'user_id': int} or None if invalid/expired."""
    try:
        parts = token.split("_", 2)
        if len(parts) != 3:
            return None
        user_id_s, ts_s, sig = parts
        ts = int(ts_s)
        if time.time() - ts > 600:   # 10-min TTL
            return None
        msg      = f"{user_id_s}:{ts_s}"
        expected = hmac.new(settings.SECRET_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected):
            return None
        return {"type": "user", "user_id": int(user_id_s)}
    except Exception:
        return None


# ── Main webhook entry point ──────────────────────────────────────────────────

@router.post("/webhook")
async def telegram_webhook(request: Request):
    """Принимает Telegram Bot API updates."""
    if _WEBHOOK_SECRET:
        incoming = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(incoming, _WEBHOOK_SECRET):
            log.warning("Telegram webhook: неверный secret token — запрос отклонён")
            return Response(status_code=403)

    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    message = update.get("message")
    if message and message.get("text"):
        try:
            await _handle_message(message)
        except Exception as e:
            log.error(f"_handle_message error: {e}", exc_info=True)
        return {"ok": True}

    callback = update.get("callback_query")
    if callback:
        try:
            await _handle_callback(callback)
        except Exception as e:
            log.error(f"_handle_callback error: {e}", exc_info=True)

    return {"ok": True}


# ── Message / command dispatcher ──────────────────────────────────────────────

async def _handle_message(message: dict):
    chat_id = str(message.get("chat", {}).get("id", ""))
    text    = message.get("text", "").strip()
    if not chat_id:
        return

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        token = parts[1].strip() if len(parts) > 1 else ""
        if token:
            await _handle_link(chat_id, token)
        else:
            await _cmd_start(chat_id)
        return

    if text.startswith("/link"):
        parts = text.split(maxsplit=1)
        code  = parts[1].strip() if len(parts) > 1 else ""
        await _handle_link_by_code(chat_id, code)
        return

    user = await _get_user_by_chat(chat_id)

    if not user:
        # Незарегистрированный — потенциальный клиент или вопрос о продукте
        await _cmd_support_unregistered(chat_id, text, message)
        return

    if text in ("/profile", "👤 Профиль"):
        await _cmd_profile(chat_id)
    elif text in ("/today", "📊 Отчёт за сегодня"):
        await _cmd_today(chat_id)
    elif text in ("/locations", "📍 Мои точки"):
        await _cmd_locations(chat_id)
    elif text in ("/alerts", "🚨 Тревоги"):
        await _cmd_alerts(chat_id)
    elif text in ("/help", "❓ Помощь"):
        await _cmd_help(chat_id)
    else:
        await _cmd_start(chat_id)


# ── Callback dispatcher ────────────────────────────────────────────────────────

async def _handle_callback(callback: dict):
    callback_id = callback.get("id", "")
    data        = callback.get("data", "")
    chat_id     = str(callback.get("message", {}).get("chat", {}).get("id", ""))

    answer_text = ""
    show_alert  = False

    try:
        if data.startswith("tc_confirm:"):
            incident_id  = int(data.split(":", 1)[1])
            answer_text  = await _handle_confirm(incident_id)

        elif data.startswith("tc_fp:"):
            incident_id  = int(data.split(":", 1)[1])
            answer_text  = await _handle_false_positive(incident_id)
            show_alert   = True

        elif data == "tc_today":
            await _cmd_today(chat_id)
        elif data == "tc_profile":
            await _cmd_profile(chat_id)
        elif data == "tc_locations":
            await _cmd_locations(chat_id)
        elif data == "tc_alerts":
            await _cmd_alerts(chat_id)
        elif data == "tc_help":
            await _cmd_help(chat_id)
        elif data == "tc_prices":
            await _cmd_prices(chat_id)
        elif data == "tc_about":
            await _cmd_about(chat_id)

    except (ValueError, IndexError):
        pass
    except Exception as e:
        log.error(f"Ошибка обработки callback '{data}': {e}")
        answer_text = "Ошибка обработки"
        show_alert  = True

    try:
        if answer_text:
            await get_bot().answer_callback_query(
                callback_query_id=callback_id,
                text=answer_text,
                show_alert=show_alert,
            )
        else:
            await get_bot().answer_callback_query(callback_query_id=callback_id)
    except Exception as e:
        log.error(f"answer_callback_query ошибка: {e}")


# ── Incident callbacks (existing behaviour) ────────────────────────────────────

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


# ── Account linking ────────────────────────────────────────────────────────────

async def _handle_link(chat_id: str, token: str):
    bot        = get_bot()
    token_data = _verify_link_token(token)

    if not token_data:
        await bot.send_message(
            chat_id=chat_id,
            text="❌ Ссылка устарела или недействительна.\n\nПолучите новую в личном кабинете.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if token_data.get("type") == "location":
        await _handle_link_location(chat_id, token_data)
    else:
        await _handle_link_user(chat_id, token_data)


async def _handle_link_user(chat_id: str, token_data: dict):
    bot     = get_bot()
    user_id = token_data["user_id"]
    log.info(f"_handle_link_user: chat_id={chat_id} user_id={user_id}")
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user   = result.scalar()
        if not user:
            log.warning(f"_handle_link_user: user_id={user_id} not found in DB")
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ Аккаунт не найден.\n\n"
                    "Пожалуйста:\n"
                    "1. Выйдите из личного кабинета\n"
                    "2. Войдите снова (или зарегистрируйтесь)\n"
                    "3. Нажмите Привязать Telegram заново"
                ),
            )
            return
        # Remove this chat_id from any other user first (prevent duplicate lookups)
        await db.execute(
            sa_update(User)
            .where(User.telegram_chat == chat_id, User.id != user_id)
            .values(telegram_chat=None)
        )
        user.telegram_chat = chat_id
        await db.commit()
        name = user.name

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ *Telegram привязан к профилю!*\n\n"
            f"Привет, *{name}*! Теперь сюда будут приходить:\n"
            f"• Уведомления о нарушениях\n"
            f"• Ежедневный итог в 22:00\n"
            f"• Тревоги с кнопками\n\n"
            f"Используйте кнопки ниже:"
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_main_keyboard(),
    )


async def _handle_link_location(chat_id: str, token_data: dict):
    bot = get_bot()
    async with AsyncSessionLocal() as db:
        loc = await db.get(Location, token_data["location_id"])
        if not loc:
            await bot.send_message(chat_id=chat_id, text="❌ Точка не найдена.")
            return
        loc.telegram_chat = chat_id
        await db.commit()
        loc_name = loc.name

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ *Telegram привязан к точке «{loc_name}»!*\n\n"
            f"Сюда будут приходить уведомления с этой кассы.\n\n"
            f"Чтобы проверить — скажите что-нибудь на кассе."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


async def _handle_link_by_code(chat_id: str, code: str):
    """Link account via 6-digit manual code (fallback for when deep link fails)."""
    bot = get_bot()
    if not code.isdigit() or len(code) != 6:
        await bot.send_message(
            chat_id=chat_id,
            text="❌ Неверный формат кода. Введите 6-значный код из личного кабинета.\n\nПример: `/link 123456`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    code_hash = hashlib.sha256(code.encode()).hexdigest()
    async with AsyncSessionLocal() as db:
        otp_result = await db.execute(
            select(OtpCode).where(
                OtpCode.code == code_hash,
                OtpCode.phone.like("tg:%"),
                OtpCode.used == False,  # noqa: E712
                OtpCode.expires_at > datetime.utcnow(),
            )
        )
        otp = otp_result.scalar()

        if not otp:
            await bot.send_message(
                chat_id=chat_id,
                text="❌ Код не найден или устарел. Получите новый код в личном кабинете → Настройки → Привязать Telegram.",
            )
            return

        user_id = int(otp.phone.split(":")[1])
        user = await db.get(User, user_id)
        if not user:
            await bot.send_message(chat_id=chat_id, text="❌ Аккаунт не найден.")
            return

        otp.used = True

        # Remove this chat_id from any other user
        await db.execute(
            sa_update(User)
            .where(User.telegram_chat == chat_id, User.id != user_id)
            .values(telegram_chat=None)
        )
        user.telegram_chat = chat_id
        await db.commit()
        name = user.name

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ *Telegram привязан к профилю!*\n\n"
            f"Привет, *{name}*! Теперь сюда будут приходить:\n"
            f"• Уведомления о нарушениях\n"
            f"• Ежедневный итог в 22:00\n"
            f"• Тревоги с кнопками\n\n"
            f"Используйте кнопки ниже:"
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_main_keyboard(),
    )


# ── Keyboards ─────────────────────────────────────────────────────────────────

def _main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📊 Отчёт за сегодня"), KeyboardButton("🚨 Тревоги")],
            [KeyboardButton("📍 Мои точки"),        KeyboardButton("👤 Профиль")],
            [KeyboardButton("❓ Помощь")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_user_by_chat(chat_id: str) -> User | None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.telegram_chat == chat_id))
        return result.scalar()


async def _no_account_msg(chat_id: str):
    await get_bot().send_message(
        chat_id=chat_id,
        text=(
            "⚠️ *Аккаунт не привязан*\n\n"
            "Войдите в *личный кабинет* → *Настройки* → нажмите *Привязать Telegram*"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


async def _send(chat_id: str, text: str, **kwargs):
    try:
        await get_bot().send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            **kwargs,
        )
    except Exception as e:
        log.error(f"Telegram send ({chat_id}): {e}")


# ── Command implementations ────────────────────────────────────────────────────

_PLAN_NAMES = {"trial": "Пробный", "start": "Старт", "business": "Бизнес", "network": "Сеть"}


_BIZ_ICONS  = {"coffee": "☕", "gas": "⛽", "fastfood": "🍔", "cafe": "🍽", "beauty": "💅", "shop": "🛍", "fitness": "💪", "hotel": "🏨"}


async def _cmd_start(chat_id: str):
    user = await _get_user_by_chat(chat_id)

    if not user:
        _app = settings.APP_URL or "https://trustcontrol.kz"
        await _send(
            chat_id,
            (
                "👋 *Привет! Это TrustControl.*\n\n"
                "ИИ следит за качеством обслуживания на вашей кассе — и присылает отчёт в Telegram.\n\n"
                "📋 *Что видите каждый день:*\n"
                "• Поздоровался ли кассир с клиентом\n"
                "• Предложил ли допродажу\n"
                "• Грубил ли — тревога мгновенно\n"
                "• Оплата прошла на чужой Kaspi? — фрод-алерт\n\n"
                "🇰🇿 Понимает казахский, русский и шала-казахский.\n\n"
                "🎁 *7 дней бесплатно — карта не нужна*"
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Попробовать бесплатно", url=f"{_app}/?ref=tg_start")],
                [InlineKeyboardButton("💰 Цены", callback_data="tc_prices"),
                 InlineKeyboardButton("❓ Как работает?", callback_data="tc_about")],
            ]),
        )
        return

    plan = _PLAN_NAMES.get(user.plan or "trial", user.plan or "trial")
    await _send(
        chat_id,
        f"👋 Привет, *{user.name}*!\n\n📦 Тариф: *{plan}*\n\nВыберите действие:",
        reply_markup=_main_keyboard(),
    )


async def _cmd_support_unregistered(chat_id: str, text: str, message: dict):
    """Автоответ потенциальному клиенту + уведомление Данила."""
    _app = settings.APP_URL or "https://trustcontrol.kz"

    # Определяем тему вопроса по ключевым словам
    tl = text.lower()
    if any(w in tl for w in ("цена", "стоимость", "сколько", "тариф", "стоит", "платить", "деньги")):
        reply = (
            "💰 *Тарифы TrustControl:*\n\n"
            "• *Старт* — 24 990 ₸/мес (1 касса)\n"
            "• *Бизнес* — 59 990 ₸/мес (до 3 касс)\n"
            "• *Поток* — 99 990 ₸/мес (до 5 касс)\n"
            "• *Сеть* — от 199 000 ₸/мес (франшизы)\n\n"
            "🎁 Первые 7 дней бесплатно — карта не нужна.\n"
            "Оплата: Kaspi-перевод, без автосписаний."
        )
    elif any(w in tl for w in ("казахск", "тіл", "язык", "шала", "қаз")):
        reply = (
            "🇰🇿 *Казахский язык*\n\n"
            "Понимаем казахский, русский и шала-казахский (code-switching).\n"
            "Для чистого распознавания в шумном зале рекомендуем направленный "
            "кардиоидный микрофон (~12 000 ₸) — поможем подобрать под ваше помещение."
        )
    elif any(w in tl for w in ("микрофон", "оборудован", "купить", "подключ", "устан", "работает")):
        reply = (
            "🎙 *Что нужно для работы:*\n\n"
            "1. Любой ПК или ноутбук на кассе (уже есть)\n"
            "2. USB-микрофон:\n"
            "   • Тихое помещение → простой, от 3 000 ₸\n"
            "   • Шумный зал → кардиоидный, ~12 000 ₸\n"
            "3. Интернет\n\n"
            "Всё. Кассовый аппарат не трогаем.\n"
            "Настройка: 10-15 минут, мы помогаем."
        )
    elif any(w in tl for w in ("слежк", "безопасн", "запис", "данные", "хранит")):
        reply = (
            "🔒 *Безопасность и конфиденциальность*\n\n"
            "Живые люди разговоры не слушают — только ИИ.\n"
            "Аудио хранится в защищённом облаке, доступно только вам.\n"
            "Обычные записи удаляются через 48 часов.\n"
            "Записи с нарушением — до 30 дней (для разбора ситуации)."
        )
    else:
        reply = (
            "Спасибо за сообщение! Данил ответит в ближайшее время.\n\n"
            "Пока — можете попробовать прямо сейчас:"
        )

    await _send(
        chat_id,
        reply,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Попробовать 7 дней бесплатно", url=f"{_app}/?ref=tg_support")],
        ]),
    )

    # Уведомляем Данила о входящем обращении
    await _notify_admin_support(chat_id, text, message)


async def _notify_admin_support(chat_id: str, text: str, message: dict):
    """Пересылает обращение незарегистрированного пользователя админу."""
    admin_chat = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
    if not admin_chat:
        # Ищем в БД: первый is_admin=True с подключённым telegram_chat
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User).where(User.is_admin == True, User.telegram_chat.isnot(None))  # noqa: E712
            )
            admin = result.scalar()
            if admin:
                admin_chat = admin.telegram_chat

    if not admin_chat or admin_chat == chat_id:
        return

    from_user = message.get("from", {})
    name_parts = [from_user.get("first_name", ""), from_user.get("last_name", "")]
    username   = from_user.get("username", "")
    name = " ".join(p for p in name_parts if p).strip() or f"id{chat_id}"
    user_ref   = f"@{username}" if username else f"[{name}](tg://user?id={chat_id})"

    try:
        await get_bot().send_message(
            chat_id=admin_chat,
            text=(
                f"📩 *Новое обращение в поддержку*\n\n"
                f"От: {user_ref}\n"
                f"Chat ID: `{chat_id}`\n\n"
                f"Вопрос:\n_{text[:300]}_"
            ),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.warning(f"Не удалось уведомить админа: {e}")


async def _cmd_profile(chat_id: str):
    user = await _get_user_by_chat(chat_id)
    if not user:
        await _no_account_msg(chat_id)
        return

    plan = _PLAN_NAMES.get(user.plan or "trial", user.plan or "trial")
    expires_str = ""
    if user.plan_expires:
        days = (user.plan_expires - datetime.utcnow()).days
        expires_str = f"\n📅 Осталось: *{max(days, 0)} дн.*" if days > 0 else "\n⚠️ Тариф истёк"

    last_login = user.last_login.strftime("%d.%m.%Y %H:%M") if user.last_login else "—"

    await _send(
        chat_id,
        (
            f"👤 *ПРОФИЛЬ*\n\n"
            f"🆔 Имя: *{user.name}*\n"
            f"📱 Телефон: `{user.phone}`\n"
            f"📦 Тариф: *{plan}*{expires_str}\n"
            f"🔐 Последний вход: {last_login}"
        ),
    )


async def _cmd_today(chat_id: str):
    user = await _get_user_by_chat(chat_id)
    if not user:
        await _no_account_msg(chat_id)
        return

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    async with AsyncSessionLocal() as db:
        locs_r = await db.execute(
            select(Location).where(Location.owner_id == user.id, Location.is_active == True)  # noqa: E712
        )
        locations = locs_r.scalars().all()

        if not locations:
            await _send(chat_id, "📍 У вас нет активных точек. Добавьте их в личном кабинете.")
            return

        lines = [f"📊 *ОТЧЁТ ЗА СЕГОДНЯ*\n_{datetime.now().strftime('%d.%m.%Y')}_\n"]
        total_all = 0

        for loc in locations:
            reps_r = await db.execute(
                select(Report).where(
                    Report.location_id == loc.id,
                    Report.timestamp   >= today_start,
                    Report.is_hidden   == False,  # noqa: E712
                )
            )
            rows  = reps_r.scalars().all()
            total = len(rows)
            total_all += total

            if total == 0:
                lines.append(f"🏪 *{loc.name}*\n   _Записей нет_\n")
                continue

            greet_pct  = sum(1 for r in rows if r.has_greeting)  / total * 100
            upsell_pct = sum(1 for r in rows if r.upsell_attempt) / total * 100
            neg_count  = sum(1 for r in rows if r.tone == "negative")
            fraud_n    = sum(1 for r in rows if r.fraud_status == "critical_fraud_risk")
            sat_list   = [r.customer_satisfaction for r in rows if r.customer_satisfaction]
            avg_sat    = sum(sat_list) / len(sat_list) if sat_list else 0.0

            block = (
                f"🏪 *{loc.name}*\n"
                f"  💬 Разговоров: {total}\n"
                f"  👋 Приветствия: {greet_pct:.0f}%\n"
                f"  🎯 Допродажи: {upsell_pct:.0f}%\n"
                f"  ⭐ Оценка: {avg_sat:.1f}/5"
            )
            if fraud_n:
                block += f"\n  🚨 Подозрения: *{fraud_n}*"
            if neg_count:
                block += f"\n  😤 Негативных: {neg_count}"
            lines.append(block + "\n")

        if total_all == 0:
            lines.append("_Записей сегодня нет_")

    await _send(chat_id, "\n".join(lines))


async def _cmd_locations(chat_id: str):
    user = await _get_user_by_chat(chat_id)
    if not user:
        await _no_account_msg(chat_id)
        return

    async with AsyncSessionLocal() as db:
        locs_r = await db.execute(select(Location).where(Location.owner_id == user.id))
        locations = locs_r.scalars().all()
        all_locs_r = await db.execute(select(Location.id, Location.name, Location.owner_id))
        all_loc_rows = all_locs_r.all()

    if not locations:
        db_type  = "pg" if "postgresql" in settings.DATABASE_URL else "sqlite"
        loc_info = ", ".join(f"id={r[0]} owner={r[2]}" for r in all_loc_rows) or "none"
        log.debug("no locations for user_id=%s db=%s locs=[%s]", user.id, db_type, loc_info)
        await _send(
            chat_id,
            (
                f"📍 У вас нет точек.\n\n"
                f"Этот Telegram привязан к аккаунту: `{user.phone}`\n\n"
                f"Если на сайте вы входите с другим номером — отвяжите Telegram "
                f"и привяжите заново из нужного аккаунта."
            ),
        )
        return

    lines = ["📍 *МОИ ТОЧКИ*\n"]
    for loc in locations:
        icon   = _BIZ_ICONS.get(loc.business_type or "", "🏪")
        status = "🟢 Активна" if loc.is_active else "🔴 Неактивна"
        if loc.telegram_chat:
            tg = "✅ Группа"
        elif user.telegram_chat:
            tg = "✅ Личный чат"
        else:
            tg = "⚠️ Telegram не настроен"
        lines.append(
            f"{icon} *{loc.name}*\n"
            f"  {status}  |  📨 {tg}\n"
            f"  🔑 `{(loc.api_key or '')[:12]}...`\n"
        )

    await _send(chat_id, "\n".join(lines))


async def _cmd_alerts(chat_id: str):
    user = await _get_user_by_chat(chat_id)
    if not user:
        await _no_account_msg(chat_id)
        return

    async with AsyncSessionLocal() as db:
        locs_r = await db.execute(
            select(Location.id).where(Location.owner_id == user.id)
        )
        loc_ids = [r[0] for r in locs_r.all()]

        if not loc_ids:
            await _send(chat_id, "📍 Нет точек.")
            return

        result = await db.execute(
            select(Incident)
            .where(Incident.location_id.in_(loc_ids), Incident.status == "open")
            .order_by(Incident.created_at.desc())
            .limit(5)
        )
        incidents = result.scalars().all()

    if not incidents:
        await _send(chat_id, "✅ *Открытых тревог нет!*\n\nВсё в порядке.")
        return

    _TYPE_ICONS = {"KASPI_FRAUD": "🚨", "FRAUD": "🚨", "AGGRESSION": "⚠️", "UPSELL_GAP": "📉"}
    lines = [f"🚨 *ОТКРЫТЫЕ ТРЕВОГИ* ({len(incidents)})\n"]
    for inc in incidents:
        icon = _TYPE_ICONS.get(inc.incident_type, "⚠️")
        ts   = inc.created_at.strftime("%d.%m %H:%M") if inc.created_at else "—"
        desc = (inc.description or "")[:80]
        lines.append(f"{icon} `{inc.incident_type}` — {ts}\n   {desc}...\n")

    lines.append("_Нажмите ✅/❌ под алертом чтобы закрыть тревогу_")
    await _send(chat_id, "\n".join(lines))


async def _cmd_prices(chat_id: str):
    _app = settings.APP_URL or "https://trustcontrol.kz"
    await _send(
        chat_id,
        (
            "💰 *Тарифы TrustControl*\n\n"
            "• *Старт* — 24 990 ₸/мес\n"
            "  1 касса, до 1 500 разговоров/мес\n\n"
            "• *Бизнес* — 59 990 ₸/мес\n"
            "  До 3 касс, аналитика по сотрудникам\n\n"
            "• *Поток* — 99 990 ₸/мес\n"
            "  До 5 касс, приоритетная поддержка\n\n"
            "• *Сеть* — от 199 000 ₸/мес\n"
            "  Франшизы, сети, индивидуальные условия\n\n"
            "🎁 Первые *7 дней бесплатно* на любом тарифе.\n"
            "Оплата Kaspi. Без карты. Без автосписаний."
        ),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Попробовать бесплатно", url=f"{_app}/?ref=tg_prices")],
        ]),
    )


async def _cmd_about(chat_id: str):
    _app = settings.APP_URL or "https://trustcontrol.kz"
    await _send(
        chat_id,
        (
            "🤖 *Как работает TrustControl*\n\n"
            "1️⃣ *USB-микрофон* на кассе → программа на кассовом ПК\n"
            "2️⃣ Каждый разговор с клиентом → ИИ распознаёт речь\n"
            "3️⃣ Анализирует: приветствие, допродажа, грубость, фрод\n"
            "4️⃣ *Отчёт в Telegram* через 15 секунд после разговора\n\n"
            "🚨 *Мгновенные тревоги:*\n"
            "  • Кассир нагрубил → пуш сразу\n"
            "  • Оплата на чужой Kaspi → фрод-алерт\n\n"
            "📊 *Дашборд:* процент приветствий, допродажи, оценки по сменам, аналитика сотрудников\n\n"
            "🇰🇿 Понимает казахский, русский, шала-казахский.\n"
            "Кассовый аппарат не трогаем."
        ),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Попробовать 7 дней бесплатно", url=f"{_app}/?ref=tg_about")],
            [InlineKeyboardButton("💰 Цены", callback_data="tc_prices")],
        ]),
    )


async def _cmd_help(chat_id: str):
    await _send(
        chat_id,
        (
            "❓ *ПОМОЩЬ*\n\n"
            "📊 *Отчёт за сегодня* — статистика по всем точкам\n"
            "🚨 *Тревоги* — открытые инциденты\n"
            "📍 *Мои точки* — список ваших касс\n"
            "👤 *Профиль* — информация об аккаунте\n\n"
            "🔔 *Уведомления приходят когда:*\n"
            "  • Обнаружена грубость на кассе\n"
            "  • Подозрение на мошенничество\n"
            "  • Ежедневный итог в 22:00\n\n"
            f"🌐 Личный кабинет:\n"
            f"{settings.APP_URL or 'https://aspanlab.onrender.com'}"
        ),
    )
