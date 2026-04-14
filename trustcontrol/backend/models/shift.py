# ════════════════════════════════════════════════════════════
#  Модель: Смена
# ════════════════════════════════════════════════════════════

from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, ForeignKey, Date, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime
from backend.database import Base


class Shift(Base):
    __tablename__ = "shifts"
    __table_args__ = (
        UniqueConstraint("location_id", "shift_date", "shift_number"),
    )

    id              = Column(Integer, primary_key=True, index=True)
    location_id     = Column(Integer, ForeignKey("locations.id"), nullable=False)

    shift_date      = Column(Date, nullable=False, index=True)
    shift_number    = Column(Integer, nullable=False)       # 1=утро 2=день 3=вечер
    shift_start     = Column(DateTime)
    shift_end       = Column(DateTime)

    # Статистика
    total_conversations = Column(Integer, default=0)

    greetings_count = Column(Integer, default=0)
    greetings_pct   = Column(Float, default=0)

    thanks_count    = Column(Integer, default=0)
    thanks_pct      = Column(Float, default=0)

    goodbye_count   = Column(Integer, default=0)
    goodbye_pct     = Column(Float, default=0)

    bonus_count     = Column(Integer, default=0)
    bonus_pct       = Column(Float, default=0)

    bad_count       = Column(Integer, default=0)
    fraud_count     = Column(Integer, default=0)

    positive_tone_count = Column(Integer, default=0)
    negative_tone_count = Column(Integer, default=0)

    # Итоговая оценка смены 0–100
    score           = Column(Float, default=0)

    location        = relationship("Location", back_populates="shifts")

    def __repr__(self):
        return f"<Shift {self.shift_date} #{self.shift_number} score={self.score}>"
