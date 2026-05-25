# ════════════════════════════════════════════════════════════
#  Модель: Торговая точка
# ════════════════════════════════════════════════════════════

from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, JSON, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from backend.database import Base


class Location(Base):
    __tablename__ = "locations"

    id              = Column(Integer, primary_key=True, index=True)
    owner_id        = Column(Integer, ForeignKey("users.id"), nullable=False)

    name            = Column(String(150), nullable=False)   # "Кофейня Арома — Касса 1"
    business_type   = Column(String(30), default="coffee")  # coffee/gas/fastfood/cafe/beauty/shop/fitness/hotel
    address         = Column(String(255))
    city            = Column(String(100), default="Алматы")

    # Telegram для этой точки
    telegram_chat   = Column(String(50))                    # отдельная группа или общая

    # Настройки мониторинга
    vad_level       = Column(Integer, default=2)            # 0-3
    silence_seconds = Column(Integer, default=3)
    language        = Column(String(10), default="ru")
    custom_phrases  = Column(JSON, default=list)            # доп. фразы владельца

    # Антифрод: настройки владельца
    allowed_phones   = Column(JSON, default=list)           # белый список Каспи-номеров
    required_upsells = Column(JSON, default=list)           # обязательные фразы допродажи

    # ── Бизнес-контекст для GPT ───────────────────────────────
    # GPT использует это для понимания специфики точки и оценки разговора
    business_description = Column(Text, nullable=True)
    # Пример: "Кофейня специалти-кофе. Наша фишка — эфиопский зерно.
    #  Гости часто спрашивают про методы заваривания."

    greeting_script = Column(Text, nullable=True)
    # Пример: "Добрый день! Что вам приготовить сегодня?"

    upsell_script = Column(Text, nullable=True)
    # Пример: "Предлагать сироп к напитку, выпечку, карту лояльности при каждом заказе"

    # ── Тумблеры отслеживания ─────────────────────────────────
    # Владелец может отключить любой параметр — он не будет влиять на оценку
    track_upsell   = Column(Boolean, default=True)   # отслеживать допродажи
    track_greeting = Column(Boolean, default=True)   # отслеживать приветствие
    track_goodbye  = Column(Boolean, default=True)   # отслеживать прощание

    # Анти-спам: Contextual Severity
    ignore_internal_profanity = Column(Boolean, default=False)
    # Мат из фонового ТВ/видео/телефона другого человека не считается нарушением сотрудника.
    # По умолчанию True — защита от ложных срабатываний.
    ignore_background_media   = Column(Boolean, default=True)
    notify_ok_conversations   = Column(Boolean, default=False)
    # notify_ok_conversations=False → Telegram только при нарушениях (рекомендуется)
    # notify_ok_conversations=True  → краткое сообщение на каждый разговор

    # Статус
    is_active       = Column(Boolean, default=True)
    api_key         = Column(String(64), unique=True)       # ключ для скрипта на кассе

    created_at      = Column(DateTime, default=datetime.utcnow)
    last_seen       = Column(DateTime)                      # последний раз скрипт прислал аудио
    last_ping_at    = Column(DateTime)                      # последний health-ping от воркера
    offline_alerted_at = Column(DateTime)                   # когда отправили offline-алерт (анти-спам)

    # Связи
    owner           = relationship("User", back_populates="locations")
    reports         = relationship("Report", back_populates="location", cascade="all, delete-orphan")
    alerts          = relationship("Alert", back_populates="location", cascade="all, delete-orphan")
    shifts          = relationship("Shift", back_populates="location", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Location {self.name}>"
