"""Ensure otp_codes.phone exists (re-guard after stamp-head skipped 0002)

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-20 00:00:00.000000

DBs that were stamped as head before 0002 ran may still have the old
otp_codes schema (email NOT NULL, no phone column). This migration
guarantees the phone column exists regardless of prior state.
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


def _index_exists(bind, index: str) -> bool:
    result = bind.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE indexname=:i"),
        {"i": index},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    bind = op.get_bind()

    # Add phone column if missing
    if not _col_exists(bind, "otp_codes", "phone"):
        op.add_column(
            "otp_codes",
            sa.Column("phone", sa.String(30), nullable=True),
        )
        # Backfill from email so we can add NOT NULL
        bind.execute(sa.text(
            "UPDATE otp_codes SET phone = COALESCE(email, 'unknown') WHERE phone IS NULL"
        ))
        op.alter_column("otp_codes", "phone", nullable=False)

    # Make email nullable (was NOT NULL in original schema)
    if _col_exists(bind, "otp_codes", "email"):
        op.alter_column("otp_codes", "email", nullable=True)

    # Add phone index if missing
    if not _index_exists(bind, "ix_otp_codes_phone"):
        op.create_index("ix_otp_codes_phone", "otp_codes", ["phone"])

    # Ensure users.phone is NOT NULL and indexed (may have been missed)
    if _col_exists(bind, "users", "phone"):
        # Fill NULLs before adding NOT NULL
        bind.execute(sa.text(
            "UPDATE users SET phone = '+700000' || LPAD(id::text, 7, '0') "
            "WHERE phone IS NULL OR phone = ''"
        ))
        op.alter_column("users", "phone",
                        existing_type=sa.String(20),
                        nullable=False)
        if not _index_exists(bind, "ix_users_phone"):
            op.create_index("ix_users_phone", "users", ["phone"], unique=True)

    # Make users.email nullable
    if _col_exists(bind, "users", "email"):
        op.alter_column("users", "email", nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    if _index_exists(bind, "ix_otp_codes_phone"):
        op.drop_index("ix_otp_codes_phone", table_name="otp_codes")
    if _index_exists(bind, "ix_users_phone"):
        op.drop_index("ix_users_phone", table_name="users")
