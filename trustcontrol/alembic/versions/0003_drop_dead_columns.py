"""Drop unused columns identified in codebase audit

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-19 00:00:00.000000

Drops columns that are never written or read by any API/service:
  reports:  duration_sec
  shifts:   shift_start, shift_end, greetings_pct, thanks_pct, goodbye_pct, bonus_pct
  payments: transaction_id, confirmed_at, confirmed_by, notes
  alerts:   resolved_by, manager_notes

All drops are guarded with IF EXISTS so this is safe to run on any DB state.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _col_exists(bind, table: str, column: str) -> bool:
    result = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name=:t AND column_name=:c"
        ),
        {"t": table, "c": column},
    )
    return result.fetchone() is not None


def _drop_if_exists(bind, table: str, column: str) -> None:
    if _col_exists(bind, table, column):
        op.drop_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()

    # reports
    _drop_if_exists(bind, "reports", "duration_sec")

    # shifts
    for col in ("shift_start", "shift_end",
                "greetings_pct", "thanks_pct", "goodbye_pct", "bonus_pct"):
        _drop_if_exists(bind, "shifts", col)

    # payments
    for col in ("transaction_id", "confirmed_at", "confirmed_by", "notes"):
        _drop_if_exists(bind, "payments", col)

    # alerts
    for col in ("resolved_by", "manager_notes"):
        _drop_if_exists(bind, "alerts", col)


def downgrade() -> None:
    op.add_column("reports", sa.Column("duration_sec", sa.Float(), nullable=True))

    op.add_column("shifts", sa.Column("shift_start",    sa.DateTime(), nullable=True))
    op.add_column("shifts", sa.Column("shift_end",      sa.DateTime(), nullable=True))
    op.add_column("shifts", sa.Column("greetings_pct",  sa.Float(),    server_default="0"))
    op.add_column("shifts", sa.Column("thanks_pct",     sa.Float(),    server_default="0"))
    op.add_column("shifts", sa.Column("goodbye_pct",    sa.Float(),    server_default="0"))
    op.add_column("shifts", sa.Column("bonus_pct",      sa.Float(),    server_default="0"))

    op.add_column("payments", sa.Column("transaction_id", sa.String(100), nullable=True))
    op.add_column("payments", sa.Column("confirmed_at",   sa.DateTime(), nullable=True))
    op.add_column("payments", sa.Column("confirmed_by",   sa.String(100), nullable=True))
    op.add_column("payments", sa.Column("notes",          sa.Text(), nullable=True))

    op.add_column("alerts", sa.Column("resolved_by",   sa.String(100), nullable=True))
    op.add_column("alerts", sa.Column("manager_notes", sa.Text(), nullable=True))
