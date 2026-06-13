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
    company_name    = Column(String(150), nullable=True)

    # Телефон — опционален. Для Telegram-самозаписи первичный идентификатор —
    # telegram_id. Телефон может быть заполнен позже в профиле (или задан
    # админом при ручном создании клиента). unique допускает несколько NULL.
    phone           = Column(String(20), unique=True, index=True, nullable=True)

    # Email — опциональный (для уведомлений, не для авторизации)
    email           = Column(String(150), nullable=True)

    # Пароль опционален: Telegram-клиенты входят без пароля (подпись виджета).
    # Заполнен только у клиентов, заведённых админом (CLI / create-client).
    hashed_password = Column(String(255), nullable=True)

    # telegram_id — первичный идентификатор для входа через Telegram Login Widget
    telegram_id     = Column(String(50), unique=True, index=True)
    telegram_chat   = Column(String(50))

    # Phone verification (OTP)
    is_verified     = Column(Boolean, default=False)

    # Подписка
    plan            = Column(String(20), default="trial")
    plan_expires    = Column(DateTime)
    is_active       = Column(Boolean, default=True)
    is_admin        = Column(Boolean, default=False)

    created_at      = Column(DateTime, default=datetime.utcnow)
    last_login      = Column(DateTime)
    last_subscription_reminder = Column(DateTime)

    # ── Реферальная программа ───────────────────────────────────────────────
    # referral_code — личный код владельца, которым он зовёт знакомых
    #                 предпринимателей (показывается в дашборде, ссылка ?ref=CODE).
    # referred_by    — id пользователя, по чьему коду этот клиент пришёл.
    # Награду за приглашение начисляем вручную (размер скидки/бонуса определим
    # после месяца работы, когда будет ясна реальная себестоимость).
    referral_code   = Column(String(12), unique=True, index=True, nullable=True)
    referred_by     = Column(Integer, index=True, nullable=True)

    locations       = relationship("Location", back_populates="owner", cascade="all, delete-orphan")
    payments        = relationship("Payment", back_populates="user")

    def __repr__(self):
        return f"<User {self.phone}>"
