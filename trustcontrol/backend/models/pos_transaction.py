# ════════════════════════════════════════════════════════════
#  Модель: POS-транзакция (данные кассового аппарата)
#
#  Используется для детектора «кассового разрыва»:
#  сопоставляем голосовые отчёты (payment_confirmed=true)
#  с реальными чеками из кассы.
# ════════════════════════════════════════════════════════════

from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from backend.database import Base


class PosTransaction(Base):
    __tablename__ = "pos_transactions"

    id           = Column(Integer, primary_key=True, index=True)
    location_id  = Column(Integer, ForeignKey("locations.id"), nullable=False, index=True)

    timestamp    = Column(DateTime, nullable=False, index=True)  # время чека из кассы
    amount       = Column(Float, nullable=False)                 # сумма в тенге
    receipt_id   = Column(String(100), nullable=True)            # ID или номер чека
    currency     = Column(String(10), default="KZT")
    cashier_id   = Column(String(100), nullable=True)            # ID кассира (если есть)
    raw_data     = Column(Text, nullable=True)                   # сырой JSON от кассы

    # Результат сопоставления с аудио-отчётом
    is_matched        = Column(Boolean, default=False, index=True)
    matched_report_id = Column(Integer, ForeignKey("reports.id"), nullable=True)

    created_at   = Column(DateTime, default=datetime.utcnow)

    location = relationship("Location")

    def __repr__(self):
        return f"<PosTransaction {self.id} amount={self.amount} @ {self.timestamp}>"
