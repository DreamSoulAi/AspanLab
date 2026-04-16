from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, ForeignKey, JSON, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from backend.database import Base


class Report(Base):
    __tablename__ = "reports"

    id              = Column(Integer, primary_key=True, index=True)
    location_id     = Column(Integer, ForeignKey("locations.id"), nullable=False)

    timestamp       = Column(DateTime, default=datetime.utcnow, index=True)
    transcript      = Column(Text, nullable=False)
    duration_sec    = Column(Float)
    audio_size_kb   = Column(Integer)

    # Найденные категории
    found_categories = Column(JSON, default=dict)

    # Флаги (для быстрой фильтрации)
    has_greeting    = Column(Boolean, default=False, index=True)
    has_thanks      = Column(Boolean, default=False)
    has_goodbye     = Column(Boolean, default=False)
    has_bonus       = Column(Boolean, default=False, index=True)
    has_bad         = Column(Boolean, default=False, index=True)
    has_fraud       = Column(Boolean, default=False, index=True)

    # Тон
    tone            = Column(String(20), default="neutral")
    tone_score      = Column(Float, default=0.5)

    # GPT-4o-mini-audio анализ
    gpt_score       = Column(Integer, nullable=True)
    gpt_summary     = Column(Text,    nullable=True)
    gpt_details     = Column(JSON,    nullable=True)

    # Диаризация
    speakers        = Column(JSON, nullable=True)

    # Смена
    shift_number    = Column(Integer)

    # Приоритет и архив
    is_priority     = Column(Boolean, default=False, index=True)
    audio_sha256    = Column(String(64), nullable=True)
    s3_url          = Column(Text, nullable=True)

    # ── Бизнес-аналитика (v3.0) ──────────────────────────────
    payment_confirmed     = Column(Boolean, nullable=True)   # оплата завершена в разговоре
    upsell_attempt        = Column(Boolean, nullable=True)   # была попытка допродажи
    customer_satisfaction = Column(Integer, nullable=True)   # настроение клиента 1-5
    is_personal_talk      = Column(Boolean, default=False, index=True)  # личный разговор
    is_hidden             = Column(Boolean, default=False, index=True)  # скрыт от дашборда

    # ── Статус мошенничества (POS-матчер) ────────────────────
    fraud_status    = Column(String(30), default="normal", index=True)
    # normal | critical_fraud_risk | cleared

    # ── S3 Retention ─────────────────────────────────────────
    s3_deleted_at   = Column(DateTime, nullable=True)        # когда файл удалён из S3

    # Связи
    location        = relationship("Location", back_populates="reports")
    alerts          = relationship("Alert", back_populates="report")

    def __repr__(self):
        return f"<Report {self.id} @ {self.timestamp}>"
