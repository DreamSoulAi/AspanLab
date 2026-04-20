"""Ensure otp_codes.phone exists (re-guard after stamp-head skipped 0002)

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-20 00:00:00.000000

Minimal fix: add phone column to otp_codes if missing.
Does NOT touch users.phone unique index (may have duplicates on existing DBs).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
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


def _index_exists(bind, index_name: str) -> bool:
    result = bind.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE indexname=:i"),
        {"i": index_name},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    bind = op.get_bind()

    # ── otp_codes: add phone column if missing ───────────────
    if not _col_exists(bind, "otp_codes", "phone"):
        op.add_column(
            "otp_codes",
            sa.Column("phone", sa.String(30), nullable=True),
        )
        # Backfill from email so we can set NOT NULL
        bind.execute(sa.text(
            "UPDATE otp_codes SET phone = COALESCE(email, 'unknown') "
            "WHERE phone IS NULL"
        ))
        op.alter_column("otp_codes", "phone", nullable=False)

    # ── otp_codes: make email nullable ───────────────────────
    if _col_exists(bind, "otp_codes", "email"):
        op.alter_column("otp_codes", "email", nullable=True)

    # ── otp_codes: add phone index if missing ────────────────
    if not _index_exists(bind, "ix_otp_codes_phone"):
        op.create_index("ix_otp_codes_phone", "otp_codes", ["phone"])


def downgrade() -> None:
    bind = op.get_bind()
    if _index_exists(bind, "ix_otp_codes_phone"):
        op.drop_index("ix_otp_codes_phone", table_name="otp_codes")
