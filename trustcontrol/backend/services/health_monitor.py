# ════════════════════════════════════════════════════════════
#  Сервис: Мониторинг онлайн-статуса воркеров
#
#  Запускается из main.py как asyncio-задача каждые 2 минуты.
#  Если точка не пинговала сервер > 10 минут — алерт в Telegram.
#  Анти-спам: повторный алерт не раньше чем через 30 минут.
# ════════════════════════════════════════════════════════════

import asyncio
import logging
from datetime import datetime, timedelta
from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.models.location import Location
from backend.models.user import User

log = logging.getLogger("health_monitor")

CHECK_INTERVAL_SEC      = 60     # проверяем каждую минуту
OFFLINE_THRESHOLD_MIN   = 10    # нет связи если нет пинга > 10 мин
ALERT_COOLDOWN_MIN      = 60    # повторное уведомление не чаще раза в час


async def _send_offline_alert(location: Location, owner: User, minutes_offline: int):
    """Отправляет Telegram-сообщение об отсутствии связи с точкой."""
    try:
        from backend.services.notifier import get_bot
        from telegram.constants import ParseMode

        chat_id = location.telegram_chat or (owner.telegram_chat if owner else None)
        if not chat_id:
            return

        hours = minutes_offline // 60
        mins  = minutes_offline % 60
        duration = f"{hours} ч {mins} мин" if hours else f"{mins} мин"

        text = (
            f"⚠️ *Нет связи с точкой*\n\n"
            f"📍 *{location.name}*\n"
            f"🕐 Нет данных уже *{duration}*\n\n"
            f"Что проверить:\n"
            f"• Интернет на кассовом компьютере\n"
            f"• Питание компьютера\n"
            f"• Программа мониторинга запущена\n\n"
            f"_Следующее уведомление — через 1 час, если связь не восстановится_"
        )
        bot = get_bot()
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
        log.info(f"[loc={location.id}] Offline-алерт отправлен в {chat_id}")
    except Exception as e:
        log.error(f"[loc={location.id}] Ошибка отправки offline-алерта: {e}")


async def run_health_monitor():
    """Бесконечный цикл проверки онлайн-статуса всех точек."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SEC)
        try:
            now = datetime.utcnow()
            offline_threshold = now - timedelta(minutes=OFFLINE_THRESHOLD_MIN)
            alert_cooldown    = now - timedelta(minutes=ALERT_COOLDOWN_MIN)

            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Location).where(
                        Location.is_active  == True,
                        Location.last_ping_at != None,   # не трогаем точки без воркера
                        Location.last_ping_at < offline_threshold,
                    )
                )
                offline_locs = result.scalars().all()

                for loc in offline_locs:
                    # Анти-спам: пропускаем если недавно уже отправляли
                    if loc.offline_alerted_at and loc.offline_alerted_at > alert_cooldown:
                        continue

                    minutes_offline = round((now - loc.last_ping_at).total_seconds() / 60)

                    # Загружаем владельца для его telegram_chat
                    owner_result = await db.execute(
                        select(User).where(User.id == loc.owner_id)
                    )
                    owner = owner_result.scalar()

                    await _send_offline_alert(loc, owner, minutes_offline)

                    loc.offline_alerted_at = now

                if offline_locs:
                    await db.commit()

        except OSError as e:
            # DNS / network unavailable (e.g. Render free tier, no Telegram token)
            log.debug(f"health_monitor сеть недоступна: {e}")
        except Exception as e:
            log.warning(f"health_monitor ошибка: {e}")
