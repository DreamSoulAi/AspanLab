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

CHECK_INTERVAL_SEC      = 30     # проверяем каждые 30 секунд (пинг тоже 30с)
OFFLINE_THRESHOLD_MIN   = 1     # оффлайн если нет пинга > 60 секунд
ALERT_COOLDOWN_MIN      = 10    # не спамим: повторный алерт через 10 мин


async def _send_offline_alert(location: Location, owner: User, minutes_offline: int):
    """Отправляет Telegram-сообщение о пропаже воркера."""
    try:
        from backend.services.notifier import get_bot
        from telegram.constants import ParseMode

        chat_id = location.telegram_chat or (owner.telegram_chat if owner else None)
        if not chat_id:
            return

        text = (
            f"📵 *ВОРКЕР УШЁЛ В ОФФЛАЙН*\n\n"
            f"🏪 *{location.name}*\n"
            f"⏱ Нет связи уже *{minutes_offline} мин*\n\n"
            f"Возможные причины:\n"
            f"  • Пропал интернет в точке\n"
            f"  • Выключился компьютер или блок питания\n"
            f"  • Скрипт monitor.py завис\n\n"
            f"_Алерт повторится если проблема не устранится_"
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

        except Exception as e:
            log.error(f"health_monitor ошибка: {e}")
