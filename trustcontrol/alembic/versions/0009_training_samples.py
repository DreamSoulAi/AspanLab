"""Training data collector: training_samples table

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-14 00:00:00.000000

Создаёт таблицу training_samples — пары (аудио в R2 + текст OpenAI) для
дообучения ISSAI (дистилляция знаний). Заполняется только при
COLLECT_TRAINING_DATA=1; по умолчанию пустая. Таблица новая, ничего не ломает.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table: str) -> bool:
    try:
        return sa.inspect(bind).has_table(table)
    except Exception:
        return False


def _index_exists(bind, name: str) -> bool:
    try:
        r = bind.execute(
            sa.text("SELECT 1 FROM pg_indexes WHERE indexname=:n"),
            {"n": name},
        )
        return r.fetchone() is not None
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()

    if not _table_exists(bind, "training_samples"):
        op.create_table(
            "training_samples",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("location_id", sa.Integer(), nullable=True),
            sa.Column("business_context", sa.String(100), nullable=True),
            sa.Column("issai_text", sa.Text(), nullable=True),
            sa.Column("openai_text", sa.Text(), nullable=False),
            sa.Column("merged_text", sa.Text(), nullable=True),
            sa.Column("gpt_status", sa.String(20), nullable=True),
            sa.Column("gpt_is_business", sa.Boolean(), nullable=True),
            sa.Column("stt_engine", sa.String(50), nullable=True),
            sa.Column("audio_key", sa.Text(), nullable=True),
            sa.Column("audio_duration_s", sa.Float(), nullable=True),
            sa.Column("quality_ok", sa.Boolean(), nullable=True),
            sa.Column("used_in_training", sa.Boolean(), nullable=True),
        )

    # Индексы (идемпотентно). Имена совпадают с index=True/Index() в модели.
    for name, cols, kw in (
        ("ix_training_samples_id", ["id"], {}),
        ("ix_training_samples_created_at", ["created_at"], {}),
        ("ix_training_samples_location_id", ["location_id"], {}),
        ("ix_training_samples_quality_ok", ["quality_ok"], {}),
        ("ix_training_samples_used_in_training", ["used_in_training"], {}),
        ("ix_ts_quality_used", ["quality_ok", "used_in_training"], {}),
        ("ix_ts_location_created", ["location_id", "created_at"], {}),
    ):
        if not _index_exists(bind, name):
            try:
                op.create_index(name, "training_samples", cols, **kw)
            except Exception:
                pass


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "training_samples"):
        op.drop_table("training_samples")
