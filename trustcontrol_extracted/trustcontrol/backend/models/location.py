# ════════════════════════════════════════════════════════════
#  Модель: Торговая точка
# ════════════════════════════════════════════════════════════

from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, JSON
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

    # Статус
    is_active       = Column(Boolean, default=True)
    api_key         = Column(String(64), unique=True)       # ключ для скрипта на кассе

    created_at      = Column(DateTime, default=datetime.utcnow)
    last_seen       = Column(DateTime)                      # последний раз скрипт был онлайн

    # Связи
    owner           = relationship("User", back_populates="locations")
    reports         = relationship("Report", back_populates="location", cascade="all, delete-orphan")
    alerts          = relationship("Alert", back_populates="location", cascade="all, delete-orphan")
    shifts          = relationship("Shift", back_populates="location", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Location {self.name}>"
