# ════════════════════════════════════════════════════════════
#  Модель: Тревога (грубость / мошенничество / нарушение)
# ════════════════════════════════════════════════════════════

from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from backend.database import Base


class Alert(Base):
    __tablename__ = "alerts"

    id              = Column(Integer, primary_key=True, index=True)
    location_id     = Column(Integer, ForeignKey("locations.id"), nullable=False)
    report_id       = Column(Integer, ForeignKey("reports.id"))

    timestamp       = Column(DateTime, default=datetime.utcnow, index=True)

    # Тип тревоги
    # fraud         — попытка принять оплату мимо кассы
    # bad_language  — грубость, мат
    # negative_tone — раздражённый тон
    # no_greeting   — не поздоровался
    # no_goodbye    — не попрощался
    alert_type      = Column(String(30), nullable=False, index=True)

    # Серьёзность
    severity        = Column(String(10), default="high")    # high / medium / low

    transcript      = Column(Text)                          # фрагмент разговора
    trigger_phrase  = Column(String(255))                   # фраза которая сработала

    # Статус
    is_resolved     = Column(Boolean, default=False)
    resolved_at     = Column(DateTime)

    # Связи
    location        = relationship("Location", back_populates="alerts")
    report          = relationship("Report", back_populates="alerts")

    def __repr__(self):
        return f"<Alert {self.alert_type} @ {self.timestamp}>"
