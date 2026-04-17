# ════════════════════════════════════════════════════════════
#  Модель: Инцидент
#
#  Создаётся автоматически при обнаружении:
#    KASPI_FRAUD  — чужой номер в разговоре с Каспи-контекстом
#    FRAUD        — мошенничество по ключевым словам
#    AGGRESSION   — грубость/конфликт (priority=1)
#    UPSELL_GAP   — допродажа прозвучала, но в чеке позиции нет
# ════════════════════════════════════════════════════════════

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from backend.database import Base


class Incident(Base):
    __tablename__ = "incidents"

    id          = Column(Integer, primary_key=True, index=True)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False, index=True)
    report_id   = Column(Integer, ForeignKey("reports.id"),   nullable=True)

    # Тип: KASPI_FRAUD | FRAUD | AGGRESSION | UPSELL_GAP
    incident_type  = Column(String(30), nullable=False, index=True)
    severity       = Column(String(20), default="high")   # low | medium | high | critical

    description    = Column(Text)

    # 15-секундный аудио-клип как доказательство
    proof_s3_url   = Column(Text,        nullable=True)
    proof_sha256   = Column(String(64),  nullable=True)

    # Kaspi-специфичные поля
    detected_phone = Column(String(30),  nullable=True)   # номер которого нет в белом списке

    # Поля для UPSELL_GAP
    upsell_phrase  = Column(String(300), nullable=True)   # что услышал ИИ
    missing_item   = Column(String(300), nullable=True)   # чего нет в чеке

    # Жизненный цикл
    status         = Column(String(20), default="open", index=True)
    # open | resolved | false_positive
    resolved_at    = Column(DateTime, nullable=True)

    created_at     = Column(DateTime, default=datetime.utcnow, index=True)

    location = relationship("Location")
    report   = relationship("Report")

    def __repr__(self):
        return f"<Incident {self.incident_type} loc={self.location_id} #{self.id}>"
