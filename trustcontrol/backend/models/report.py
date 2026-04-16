# ════════════════════════════════════════════════════════════
#  Модель: Отчёт о разговоре
# ════════════════════════════════════════════════════════════

from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, ForeignKey, JSON, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from backend.database import Base


class Report(Base):
    __tablename__ = "reports"

    id              = Column(Integer, primary_key=True, index=True)
    location_id     = Column(Integer, ForeignKey("locations.id"), nullable=False)

    timestamp       = Column(DateTime, default=datetime.utcnow, index=True)
    transcript      = Column(Text, nullable=False)          # текст разговора
    duration_sec    = Column(Float)                         # длительность записи
    audio_size_kb   = Column(Integer)                       # размер аудио

    # Найденные категории
    found_categories = Column(JSON, default=dict)           # {"✅ Приветствие": ["привет"]}

    # Флаги (для быстрой фильтрации)
    has_greeting    = Column(Boolean, default=False, index=True)
    has_thanks      = Column(Boolean, default=False)
    has_goodbye     = Column(Boolean, default=False)
    has_bonus       = Column(Boolean, default=False, index=True)
    has_bad         = Column(Boolean, default=False, index=True)
    has_fraud       = Column(Boolean, default=False, index=True)

    # Тон
    tone            = Column(String(20), default="neutral") # positive/negative/neutral
    tone_score      = Column(Float, default=0.5)            # 0.0 — очень негативный, 1.0 — очень позитивный

    # GPT-4o-mini анализ
    gpt_score       = Column(Integer, nullable=True)        # оценка качества 0-100 от GPT
    gpt_summary     = Column(Text,    nullable=True)        # краткое резюме от GPT
    gpt_details     = Column(JSON,    nullable=True)        # {"positives": [...], "issues": [...]}

    # Диаризация (кто говорит)
    speakers        = Column(JSON, nullable=True)           # [{"role":"cashier","text":"..."},...]

    # Смена
    shift_number    = Column(Integer)                       # 1/2/3

    # Связи
    location        = relationship("Location", back_populates="reports")
    alerts          = relationship("Alert", back_populates="report")

    def __repr__(self):
        return f"<Report {self.id} @ {self.timestamp}>"
