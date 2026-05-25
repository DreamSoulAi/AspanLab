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
    elif text.startswith("/link"):
        parts = text.split(maxsplit=1)
        code  = parts[1].strip() if len(parts) > 1 else ""
        await _handle_link_by_code(chat_id, code)
    elif text in ("/profile", "👤 Профиль"):
        await _cmd_profile(chat_id)
    elif text in ("/today", "📊 Отчёт за сегодня"):
        await _cmd_today(chat_id)
    elif text in ("/locations", "📍 Мои точки"):
        await _cmd_locations(chat_id)
    elif text in ("/alerts", "🚨 Тревоги"):
        await _cmd_alerts(chat_id)
    elif text in ("/help", "❓ Помощь"):
        await _cmd_help(chat_id)
    elif text == "/debug":
        await _cmd_debug(chat_id)
    elif text == "/verify":
        await _cmd_verify(chat_id)
    elif text.startswith("/setpassword"):
        parts = text.split(maxsplit=1)
        new_pw = parts[1].strip() if len(parts) > 1 else ""
        await _cmd_setpassword(chat_id, new_pw)
    elif text.startswith("/simlogin"):
        parts = text.split(maxsplit=1)
        pw = parts[1].strip() if len(parts) > 1 else ""
        await _cmd_simlogin(chat_id, pw)
    elif text == "/gettoken":
        await _cmd_gettoken(chat_id)
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

    async with AsyncSessionLocal() as db:
        otp_result = await db.execute(
            select(OtpCode).where(
                OtpCode.code == code,
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


async def _cmd_gettoken(chat_id: str):
    """Generate a one-time login URL for the linked account."""
    user = await _get_user_by_chat(chat_id)
    if not user:
        await _send(chat_id, "❌ Нет привязанного аккаунта.")
        return
    token   = generate_link_token({"type": "user", "user_id": user.id})
    app_url = (settings.APP_URL or "https://aspanlab.onrender.com").rstrip("/")
    url     = f"{app_url}/?login_token={token}"
    await _send(
        chat_id,
        f"🔑 *Войти без пароля*\n\n[Нажми сюда чтобы войти]({url})\n\n_Ссылка действует 10 минут_",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 Открыть личный кабинет", url=url),
        ]]),
    )
_BIZ_ICONS  = {"coffee": "☕", "gas": "⛽", "fastfood": "🍔", "cafe": "🍽", "beauty": "💅", "shop": "🛍", "fitness": "💪", "hotel": "🏨"}


async def _cmd_simlogin(chat_id: str, pw: str):
    """Simulate exactly what the web /login endpoint does, report result."""
    from backend.api.auth import normalize_phone, verify_password
    async with AsyncSessionLocal() as db:
        # Find user by chat_id to get their phone
        user_r = await db.execute(select(User).where(User.telegram_chat == chat_id))
        tg_user = user_r.scalar()
        if not tg_user:
            await _send(chat_id, "❌ Нет привязанного аккаунта.")
            return
        phone = tg_user.phone

        # Now simulate web login: look up by phone (same as web does)
        phone_norm = normalize_phone(phone) or phone.strip()
        web_r = await db.execute(select(User).where(User.phone == phone_norm))
        web_user = web_r.scalar()

        if not web_user:
            await _send(chat_id, f"❌ По телефону `{phone_norm}` пользователь не найден")
            return

        pw_ok      = verify_password(pw, web_user.hashed_password)
        hash_start = (web_user.hashed_password or "")[:20]
        same_id    = tg_user.id == web_user.id

        lines = [
            "🔍 *Симуляция логина*",
            f"phone: `{phone_norm}`",
            f"same user id: `{same_id}` (tg={tg_user.id} web={web_user.id})",
            f"hash\\[0:20\\]: `{hash_start}`",
            f"verified: `{web_user.is_verified}`",
            f"is\\_active: `{web_user.is_active}`",
            f"verify\\_password: `{pw_ok}`",
        ]
        await _send(chat_id, "\n".join(lines))


async def _cmd_setpassword(chat_id: str, new_pw: str):
    """Reset password for the linked account via Telegram."""
    from backend.api.auth import hash_password, verify_password
    if len(new_pw) < 8:
        await _send(chat_id, "❌ Пароль должен быть минимум 8 символов.\n\nПример: `/setpassword МойПароль123`")
        return
    new_hash = hash_password(new_pw)
    async with AsyncSessionLocal() as db:
        user_r = await db.execute(select(User).where(User.telegram_chat == chat_id))
        user   = user_r.scalar()
        if not user:
            await _send(chat_id, "❌ Telegram не привязан к аккаунту.")
            return
        user_id = user.id
        await db.execute(
            sa_update(User)
            .where(User.id == user_id)
            .values(hashed_password=new_hash)
        )
        await db.commit()
        # Re-fetch to verify the update persisted
        check_r = await db.execute(select(User.hashed_password).where(User.id == user_id))
        stored  = check_r.scalar()
    ok = verify_password(new_pw, stored) if stored else False
    if ok:
        await _send(chat_id, "✅ *Пароль обновлён и проверен!*\n\nТеперь войдите на сайте с новым паролем.")
    else:
        await _send(chat_id, f"⚠️ Пароль записан, но проверка не прошла.\nhash\\[0:20\\]: `{(stored or '')[:20]}`")


async def _cmd_verify(chat_id: str):
    """Verify account directly via Telegram (trust: Telegram already linked = phone confirmed)."""
    async with AsyncSessionLocal() as db:
        user_r = await db.execute(select(User).where(User.telegram_chat == chat_id))
        user   = user_r.scalar()
        if not user:
            await _send(chat_id, "❌ Telegram не привязан к аккаунту.\nСначала привяжите через сайт → Настройки → Привязать Telegram.")
            return
        if user.is_verified:
            await _send(chat_id, "✅ Ваш аккаунт уже подтверждён. Войдите на сайте.")
            return
        user.is_verified = True
        await db.commit()
    await _send(chat_id, "✅ *Аккаунт подтверждён!*\n\nТеперь войдите на сайте с номером и паролем.")


async def _cmd_debug(chat_id: str):
    """Diagnostic command to identify account/location mismatch."""
    async with AsyncSessionLocal() as db:
        user_r = await db.execute(select(User).where(User.telegram_chat == chat_id))
        user   = user_r.scalar()

        if not user:
            await _send(chat_id, f"`debug: chat_id={chat_id} → no user found`")
            return

        locs_r = await db.execute(select(Location).where(Location.owner_id == user.id))
        locs   = locs_r.scalars().all()

        all_locs_r = await db.execute(select(Location.id, Location.owner_id, Location.name, Location.is_active))
        all_locs   = all_locs_r.all()

        all_users_r = await db.execute(select(User.id, User.phone, User.is_verified))
        all_users   = all_users_r.all()

    db_type = "PostgreSQL" if "postgresql" in settings.DATABASE_URL else f"SQLite ({settings.DATABASE_URL})"

    lines = [
        f"🔍 *DEBUG*\n",
        f"DB: `{db_type}`",
        f"chat\\_id: `{chat_id}`",
        f"user: id=`{user.id}` phone=`{user.phone}` verified=`{user.is_verified}`",
        f"locations for this user: `{len(locs)}`",
        f"\nВсе точки в БД ({len(all_locs)}):",
    ]
    for loc in all_locs:
        lines.append(f"  id={loc[0]} owner={loc[1]} active={loc[3]} `{loc[2]}`")

    lines.append(f"\nВсе пользователи ({len(all_users)}):")
    for u in all_users:
        lines.append(f"  id={u[0]} phone=`{u[1]}` verified={u[2]}")

    await _send(chat_id, "\n".join(lines))


async def _cmd_start(chat_id: str):
    user = await _get_user_by_chat(chat_id)

    if not user:
        await _send(
            chat_id,
            (
                "👋 *Добро пожаловать в TrustControl!*\n\n"
                "Для получения уведомлений привяжите этот Telegram к вашему аккаунту:\n\n"
                "1. Войдите в *личный кабинет*\n"
                "2. Перейдите в *Настройки*\n"
                "3. Нажмите *Привязать Telegram*"
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🌐 Открыть личный кабинет", url=settings.APP_URL or "https://aspanlab.onrender.com"),
            ]]),
        )
        return

    plan = _PLAN_NAMES.get(user.plan or "trial", user.plan or "trial")
    await _send(
        chat_id,
        f"👋 Привет, *{user.name}*!\n\n📦 Тариф: *{plan}*\n\nВыберите действие:",
        reply_markup=_main_keyboard(),
    )


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
