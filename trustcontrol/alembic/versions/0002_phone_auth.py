"""Phone-based auth: phone becomes primary key in users, otp_codes switches to phone

Revision ID: 0002
Revises: 0001
Create Date: 2024-01-02 00:00:00.000000

Safe to run on existing Render DB:
- users.phone gets UNIQUE + NOT NULL (NULL rows get placeholder +70000000<id>)
- users.email drops NOT NULL constraint (becomes optional)
- otp_codes.phone column added (email becomes nullable for old rows)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table: str) -> bool:
    result = bind.execute(
        sa.text("SELECT 1 FROM information_schema.tables WHERE table_name=:t"),
        {"t": table},
    )
    return result.fetchone() is not None


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
        sa.text(
            "SELECT 1 FROM pg_indexes WHERE indexname=:i"
        ),
        {"i": index},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    bind = op.get_bind()

    # ── users: phone → unique, not null ─────────────────────
    # 1. Fill NULL phones with placeholder so we can add NOT NULL
    bind.execute(sa.text(
        "UPDATE users SET phone = '+700000' || LPAD(id::text, 7, '0') "
        "WHERE phone IS NULL OR phone = ''"
    ))

    # 2. Drop old unique index on email (email becomes optional)
    if _index_exists(bind, "ix_users_email"):
        op.drop_index("ix_users_email", table_name="users")

    # 3. Make email nullable
    op.alter_column("users", "email", nullable=True)

    # 4. Make phone not null
    op.alter_column("users", "phone",
                    existing_type=sa.String(20),
                    nullable=False)

    # 5. Add unique index on phone (if not already exists)
    if not _index_exists(bind, "ix_users_phone"):
        op.create_index("ix_users_phone", "users", ["phone"], unique=True)

    # ── otp_codes: add phone column ──────────────────────────
    if not _col_exists(bind, "otp_codes", "phone"):
        op.add_column(
            "otp_codes",
            sa.Column("phone", sa.String(30), nullable=True),
        )
        # Migrate existing rows: copy email → phone as placeholder
        bind.execute(sa.text(
            "UPDATE otp_codes SET phone = COALESCE(email, 'unknown') WHERE phone IS NULL"
        ))
        # Now make phone not null
        op.alter_column("otp_codes", "phone", nullable=False)

    # Make email nullable on otp_codes (keep for old rows)
    if _col_exists(bind, "otp_codes", "email"):
        op.alter_column("otp_codes", "email", nullable=True)

    # Add index on otp_codes.phone
    if not _index_exists(bind, "ix_otp_codes_phone"):
        op.create_index("ix_otp_codes_phone", "otp_codes", ["phone"])


def downgrade() -> None:
    bind = op.get_bind()

    if _index_exists(bind, "ix_otp_codes_phone"):
        op.drop_index("ix_otp_codes_phone", table_name="otp_codes")

    if _index_exists(bind, "ix_users_phone"):
        op.drop_index("ix_users_phone", table_name="users")

    op.alter_column("users", "phone", nullable=True)
    op.alter_column("users", "email", nullable=False)
    op.create_index("ix_users_email", "users", ["email"], unique=True)
