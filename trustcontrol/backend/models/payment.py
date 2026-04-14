# ════════════════════════════════════════════════════════════
#  Модель: Платёж
# ════════════════════════════════════════════════════════════

from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from backend.database import Base


class Payment(Base):
    __tablename__ = "payments"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=False)

    amount          = Column(Float, nullable=False)         # сумма в тенге
    currency        = Column(String(3), default="KZT")
    plan            = Column(String(20))                    # start / business / network
    period_months   = Column(Integer, default=1)

    # Статус
    status          = Column(String(20), default="pending") # pending / confirmed / failed
    payment_method  = Column(String(30), default="kaspi")   # kaspi / card

    # Kaspi
    kaspi_phone     = Column(String(20))                    # номер отправителя
    screenshot_path = Column(String(255))                   # путь к скрину чека
    transaction_id  = Column(String(100))                   # ID транзакции

    # Даты
    created_at      = Column(DateTime, default=datetime.utcnow)
    confirmed_at    = Column(DateTime)
    confirmed_by    = Column(String(100))                   # кто подтвердил
    notes           = Column(Text)

    user            = relationship("User", back_populates="payments")

    def __repr__(self):
        return f"<Payment {self.amount}₸ {self.status}>"
