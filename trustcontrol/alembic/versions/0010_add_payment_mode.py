"""Add payment_mode to locations

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-17 00:00:00.000000

Добавляет поле payment_mode в таблицу locations.
Значения: qr_only | cash_only | transfers_ok | mixed (дефолт mixed).
Используется kaspi_detector для выбора уровня строгости фрод-проверки.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "locations",
        sa.Column("payment_mode", sa.String(20), nullable=True, server_default="mixed"),
    )


def downgrade() -> None:
    op.drop_column("locations", "payment_mode")
