"""Add is_primary to reports

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-17 00:00:00.000000

Добавляет поле is_primary в таблицу reports.
Одна запись с кассы (один submit) может разбиваться на несколько диалогов
(несколько клиентов) → отдельный Report на клиента. is_primary=True у первого
(или единственного), False у доп. диалогов записи. Месячный лимит тарифа
считает только is_primary (биллинг «по записям»), дашборд/итог — все строки.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "reports",
        sa.Column("is_primary", sa.Boolean(), nullable=True, server_default=sa.true()),
    )
    op.create_index("ix_reports_is_primary", "reports", ["is_primary"])


def downgrade() -> None:
    op.drop_index("ix_reports_is_primary", table_name="reports")
    op.drop_column("reports", "is_primary")
