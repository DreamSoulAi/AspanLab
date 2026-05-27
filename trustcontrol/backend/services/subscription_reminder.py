"""
Daily background loop that sends Telegram subscription reminders.

Triggers:
  • days_left ≤ 3 and > 0     → "expires in N days"
  • status == "grace"          → "expired, you have N grace days"
  • status == "blocked"        → "blocked, pay to resume"

Each user gets at most one reminder per 24h to avoid spam.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.models.user import User
from backend.services.subscription import get_status, days_left, GRACE_DAYS

log = logging.getLogger("subscription_reminder")

CHECK_INTERVAL_SEC      = 60 * 60      # каждый час
REMINDER_COOLDOWN_HOURS = 24           # одно напоминание в сутки


def _build_message(user: User) -> str | None:
    status = get_status(user)
    left   = days_left(user)

    if status == "active":
        if left == 3:
            return (
                f"⏰ *Подписка TrustControl*\n\n"
                f"Истекает через *3 дня*.\n"
                f"Чтобы мониторинг не остановился — оплатите в личном кабинете."
            )
        if left == 1:
            return (
                f"⏰ *Подписка TrustControl*\n\n"
                f"Истекает *завтра*.\n"
                f"Оплатите в личном кабинете чтобы продолжить работу."
            )
        return None

    if status == "grace":
        # сколько ещё дней работаем после истечения
        if not user.plan_expires:
            return None
        grace_left = GRACE_DAYS - (datetime.utcnow() - user.plan_expires).days
        return (
            f"⚠️ *Подписка истекла*\n\n"
            f"Мониторинг продолжит работу ещё *{grace_left} дн.* — это льготный период.\n"
            f"Оплатите в личном кабинете чтобы не потерять сервис."
        )

    if status == "blocked":
        return (
            f"🔒 *Подписка заблокирована*\n\n"
            f"Сбор разговоров остановлен.\n"
            f"Оплатите в личном кабинете для возобновления."
        )

    return None


async def _send(user: User, text: str) -> bool:
    if not user.telegram_chat:
        return False
    try:
        from backend.services.notifier import get_bot
        from telegram.constants import ParseMode
        bot = get_bot()
        await bot.send_message(
            chat_id=user.telegram_chat,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )
        return True
    except Exception as e:
        log.warning(f"[user={user.id}] send failed: {e}")
        return False


async def run_subscription_reminder():
    """Бесконечный цикл — раз в час проверяет всех юзеров."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SEC)
        try:
            cutoff = datetime.utcnow() - timedelta(hours=REMINDER_COOLDOWN_HOURS)

            async with AsyncSessionLocal() as db:
                # берём только активных верифицированных юзеров с TG чатом
                result = await db.execute(
                    select(User).where(
                        User.is_active    == True,
                        User.is_verified  == True,
                        User.telegram_chat != None,
                    )
                )
                users = result.scalars().all()

                sent_count = 0
                for u in users:
                    if u.is_admin:
                        continue
                    if u.last_subscription_reminder and u.last_subscription_reminder > cutoff:
                        continue
                    msg = _build_message(u)
                    if not msg:
                        continue
                    if await _send(u, msg):
                        u.last_subscription_reminder = datetime.utcnow()
                        sent_count += 1

                if sent_count:
                    await db.commit()
                    log.info(f"Subscription reminders sent: {sent_count}")

        except OSError as e:
            log.debug(f"subscription_reminder сеть недоступна: {e}")
        except Exception as e:
            log.warning(f"subscription_reminder ошибка: {e}")
