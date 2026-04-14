# ════════════════════════════════════════════════════════════
#  Сервис: Структурированное логирование
# ════════════════════════════════════════════════════════════

import logging
import sys
from datetime import datetime
from backend.config import settings


def setup_logging():
    """Настраиваем логирование для всего приложения."""
    level = logging.DEBUG if settings.DEBUG else logging.INFO

    # Формат для продакшна — JSON-like, удобно для парсинга
    if not settings.DEBUG:
        fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    else:
        # В дев режиме — читаемый формат с цветами
        fmt = "%(asctime)s \033[36m%(name)s\033[0m %(levelname)s: %(message)s"

    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ]
    )

    # Уменьшаем шум от сторонних библиотек
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DEBUG else logging.WARNING
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)

    return logging.getLogger("trustcontrol")


def get_logger(name: str) -> logging.Logger:
    """Получаем логгер для модуля."""
    return logging.getLogger(f"trustcontrol.{name}")
