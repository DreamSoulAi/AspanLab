# ════════════════════════════════════════════════════════════
#  Модель: Очередь повторной обработки
#
#  Когда OpenAI недоступен или возвращает ошибку,
#  задача сохраняется здесь и повторяется через 5 минут.
#  Аудио-файл хранится на диске в uploads/retry/.
# ════════════════════════════════════════════════════════════

from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, Text
from datetime import datetime
from backend.database import Base


class FailedJob(Base):
    __tablename__ = "failed_jobs"

    id             = Column(Integer, primary_key=True, index=True)
    location_id    = Column(Integer, nullable=False, index=True)

    # Путь к аудио-файлу на диске (uploads/retry/<uuid>.wav)
    audio_path     = Column(String(500), nullable=True)
    transcript_text = Column(Text, nullable=True)        # если был текстовый режим
    language       = Column(String(10), nullable=True)
    audio_size_kb  = Column(Integer, default=0)

    # Метаданные точки (нужны для повтора без обращения к HTTP-сессии)
    business_type  = Column(String(50), nullable=True)
    custom_phrases = Column(JSON, default=list)
    telegram_chat  = Column(String(100), nullable=True)
    location_name  = Column(String(200), nullable=True)

    # Управление повторами
    retry_count    = Column(Integer, default=0)
    next_retry_at  = Column(DateTime, nullable=False, index=True)
    last_error     = Column(Text, nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow)

    # Статус: pending | processing | failed_permanently
    status         = Column(String(20), default="pending", index=True)

    def __repr__(self):
        return f"<FailedJob {self.id} loc={self.location_id} retries={self.retry_count}>"
