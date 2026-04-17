# ════════════════════════════════════════════════════════════
#  Модель: Пользователь (владелец бизнеса)
# ════════════════════════════════════════════════════════════

from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from backend.database import Base


class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    name            = Column(String(100), nullable=False)
    email           = Column(String(150), unique=True, index=True, nullable=False)
    phone           = Column(String(20))
    hashed_password = Column(String(255), nullable=False)
    telegram_id     = Column(String(50))       # Telegram ID владельца
    telegram_chat   = Column(String(50))       # ID группы для уведомлений

    # Email verification
    is_verified     = Column(Boolean, default=False)

    # Подписка
    plan            = Column(String(20), default="trial")  # trial / start / business / network
    plan_expires    = Column(DateTime)
    is_active       = Column(Boolean, default=True)
    is_admin        = Column(Boolean, default=False)

    created_at      = Column(DateTime, default=datetime.utcnow)
    last_login      = Column(DateTime)

    # Связи
    locations       = relationship("Location", back_populates="owner", cascade="all, delete-orphan")
    payments        = relationship("Payment", back_populates="user")

    def __repr__(self):
        return f"<User {self.email}>"
