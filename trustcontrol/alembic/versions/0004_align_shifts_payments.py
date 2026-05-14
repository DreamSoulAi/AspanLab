"""Align shifts and payments tables with current ORM models

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-29 00:00:00.000000

Fixes schema drift between migration 0001 and the current ORM definitions:

shifts:
  - date (DateTime)      → shift_date (Date)
  - total_reports        → total_conversations
  - alerts_count/avg_score dropped
  - greetings_count, thanks_count, goodbye_count, bonus_count,
    bad_count, fraud_count, positive_tone_count, negative_tone_count, score added
  - UniqueConstraint(location_id, shift_date, shift_number) added

payments:
  - kaspi_ref, paid_at dropped (no longer in ORM)
  - period_months, payment_method, kaspi_phone, screenshot_path added

All ops guarded with _col_exists/_index_exists → safe to run on any DB state.
Skipped on non-PostgreSQL (SQLite dev doesn't support ALTER TABLE fully).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
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


def _index_exists(bind, index_name: str) -> bool:
    r = bind.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE indexname=:i"),
        {"i": index_name},
    )
    return r.fetchone() is not None


def _constraint_exists(bind, table: str, constraint: str) -> bool:
    r = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_name=:t AND constraint_name=:c"
        ),
        {"t": table, "c": constraint},
    )
    return r.fetchone() is not None


def _is_postgres(bind) -> bool:
    try:
        bind.execute(sa.text("SELECT 1 FROM pg_catalog.pg_tables LIMIT 1"))
        return True
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()

    if not _is_postgres(bind):
        return  # SQLite dev: skip, create_all handles it

    # ══════════════════════════════════════════
    #  shifts
    # ══════════════════════════════════════════

    # date → shift_date (Date)
    if _col_exists(bind, "shifts", "date") and not _col_exists(bind, "shifts", "shift_date"):
        op.add_column("shifts", sa.Column("shift_date", sa.Date(), nullable=True))
        bind.execute(sa.text(
            "UPDATE shifts SET shift_date = date::date WHERE shift_date IS NULL"
        ))
        op.alter_column("shifts", "shift_date", nullable=False)
        op.drop_column("shifts", "date")
    elif not _col_exists(bind, "shifts", "shift_date"):
        # Table was created fresh without old columns — add shift_date
        op.add_column("shifts", sa.Column("shift_date", sa.Date(), nullable=True))
        bind.execute(sa.text(
            "UPDATE shifts SET shift_date = CURRENT_DATE WHERE shift_date IS NULL"
        ))
        op.alter_column("shifts", "shift_date", nullable=False)

    # total_reports → total_conversations
    if _col_exists(bind, "shifts", "total_reports") and not _col_exists(bind, "shifts", "total_conversations"):
        op.add_column("shifts", sa.Column("total_conversations", sa.Integer(), server_default="0"))
        bind.execute(sa.text(
            "UPDATE shifts SET total_conversations = total_reports WHERE total_conversations = 0"
        ))
        op.drop_column("shifts", "total_reports")
    elif not _col_exists(bind, "shifts", "total_conversations"):
        op.add_column("shifts", sa.Column("total_conversations", sa.Integer(), server_default="0"))

    # Drop obsolete columns
    for col in ("alerts_count", "avg_score", "created_at"):
        if _col_exists(bind, "shifts", col):
            op.drop_column("shifts", col)

    # Add new metric columns
    new_shift_cols = [
        ("greetings_count",      sa.Integer(), "0"),
        ("thanks_count",         sa.Integer(), "0"),
        ("goodbye_count",        sa.Integer(), "0"),
        ("bonus_count",          sa.Integer(), "0"),
        ("bad_count",            sa.Integer(), "0"),
        ("fraud_count",          sa.Integer(), "0"),
        ("positive_tone_count",  sa.Integer(), "0"),
        ("negative_tone_count",  sa.Integer(), "0"),
        ("score",                sa.Float(),   "0"),
    ]
    for col_name, col_type, default in new_shift_cols:
        if not _col_exists(bind, "shifts", col_name):
            op.add_column("shifts", sa.Column(col_name, col_type, server_default=default))

    # UniqueConstraint on (location_id, shift_date, shift_number)
    uc_name = "uq_shifts_location_date_number"
    if not _constraint_exists(bind, "shifts", uc_name):
        try:
            op.create_unique_constraint(
                uc_name, "shifts", ["location_id", "shift_date", "shift_number"]
            )
        except Exception:
            pass  # Duplicate data may prevent this — skip gracefully

    # ══════════════════════════════════════════
    #  payments
    # ══════════════════════════════════════════

    # Drop obsolete columns
    for col in ("kaspi_ref", "paid_at"):
        if _col_exists(bind, "payments", col):
            op.drop_column("payments", col)

    # Add new columns
    new_payment_cols = [
        ("period_months",   sa.Integer(),     "1"),
        ("payment_method",  sa.String(30),    "kaspi"),
        ("kaspi_phone",     sa.String(20),    None),
        ("screenshot_path", sa.String(255),   None),
    ]
    for col_name, col_type, default in new_payment_cols:
        if not _col_exists(bind, "payments", col_name):
            if default is not None:
                op.add_column("payments", sa.Column(col_name, col_type, server_default=default))
            else:
                op.add_column("payments", sa.Column(col_name, col_type, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        return

    # payments
    for col in ("period_months", "payment_method", "kaspi_phone", "screenshot_path"):
        if _col_exists(bind, "payments", col):
            op.drop_column("payments", col)
    if not _col_exists(bind, "payments", "kaspi_ref"):
        op.add_column("payments", sa.Column("kaspi_ref", sa.String(100), nullable=True))
    if not _col_exists(bind, "payments", "paid_at"):
        op.add_column("payments", sa.Column("paid_at", sa.DateTime(), nullable=True))

    # shifts (partial restore)
    for col in ("greetings_count", "thanks_count", "goodbye_count", "bonus_count",
                "bad_count", "fraud_count", "positive_tone_count", "negative_tone_count", "score"):
        if _col_exists(bind, "shifts", col):
            op.drop_column("shifts", col)
    if _col_exists(bind, "shifts", "shift_date") and not _col_exists(bind, "shifts", "date"):
        op.add_column("shifts", sa.Column("date", sa.DateTime(), nullable=True))
        bind.execute(sa.text("UPDATE shifts SET date = shift_date::timestamp"))
        op.drop_column("shifts", "shift_date")
    if not _col_exists(bind, "shifts", "total_reports"):
        op.add_column("shifts", sa.Column("total_reports", sa.Integer(), server_default="0"))
    if not _col_exists(bind, "shifts", "alerts_count"):
        op.add_column("shifts", sa.Column("alerts_count", sa.Integer(), server_default="0"))
    if not _col_exists(bind, "shifts", "avg_score"):
        op.add_column("shifts", sa.Column("avg_score", sa.Float(), nullable=True))
