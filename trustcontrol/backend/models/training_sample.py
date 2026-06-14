from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, Text, Index
from datetime import datetime
from backend.database import Base


class TrainingSample(Base):
    """
    Пара (аудио + текст OpenAI) для дообучения ISSAI под твой домен.

    Схема «дистилляция знаний»:
      OpenAI gpt-4o-transcribe (учитель) → пишет правильный текст
      ISSAI whisper-turbo-ksc2 (ученик) → пишет что успел понять
      Пара (wav в R2, тексты в БД) → датасет для LoRA fine-tuning

    Аудио хранится в R2/S3 под ключом audio_key.
    Если R2 не настроен — quality_ok=False, аудио не сохраняется
    (только тексты; для обучения нужен аудио-файл, такие строки пропускают).
    """
    __tablename__ = "training_samples"

    id              = Column(Integer, primary_key=True, index=True)
    created_at      = Column(DateTime, default=datetime.utcnow, index=True)

    location_id     = Column(Integer, nullable=True, index=True)   # какая касса (для domain-specific дообучения)
    business_context = Column(String(100), nullable=True)          # coffee/cafe/fastfood/…

    # ── Тексты (суть датасета) ─────────────────────────────────────────────
    issai_text      = Column(Text, nullable=True)   # что услышал ученик (сырой, с ошибками)
    openai_text     = Column(Text, nullable=False)  # что услышал учитель (ground truth)
    merged_text     = Column(Text, nullable=True)   # финальный merge (для справки)

    # ── Метка GPT-анализа ─────────────────────────────────────────────────
    gpt_status      = Column(String(20), nullable=True)   # OK / PERSONAL / IGNORE
    gpt_is_business = Column(Boolean, nullable=True)
    stt_engine      = Column(String(50), nullable=True)   # cascade_hybrid / gpt-4o-mini-transcribe

    # ── Аудио в R2/S3 ─────────────────────────────────────────────────────
    audio_key       = Column(Text, nullable=True)          # R2 ключ: training/2026/06/14/uuid.wav
    audio_duration_s = Column(Float, nullable=True)        # длина аудио в секундах

    # ── Контроль качества ─────────────────────────────────────────────────
    # quality_ok=True → пара пригодна для обучения:
    #   • openai_text непустой и ≥4 слов (минимальная значимая транскрипция)
    #   • аудио загружено в R2 (audio_key не None)
    quality_ok      = Column(Boolean, default=False, index=True)

    # ── Статус использования в обучении ──────────────────────────────────
    used_in_training = Column(Boolean, default=False, index=True)  # отмечается скриптом fine-tuning

    __table_args__ = (
        Index("ix_ts_quality_used", "quality_ok", "used_in_training"),
        Index("ix_ts_location_created", "location_id", "created_at"),
    )
