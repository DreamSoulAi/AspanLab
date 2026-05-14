"""Add notify_ok_conversations to locations

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-14 00:00:00.000000

По умолчанию False — Telegram получает только нарушения, не каждый разговор.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _col_exists(bind, table: str, column: str) -> bool:
    r = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name=:t AND column_name=:c"
        ),
        {"t": table, "c": column},
    )
    return r.fetchone() is not None


def upgrade() -> None:
    bind = op.get_bind()
    if not _col_exists(bind, "locations", "notify_ok_conversations"):
        op.add_column(
            "locations",
            sa.Column("notify_ok_conversations", sa.Boolean(), server_default="false", nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _col_exists(bind, "locations", "notify_ok_conversations"):
        op.drop_column("locations", "notify_ok_conversations")
