"""Post-launch schema: users/otp_codes nullable fixes, new columns for locations & reports

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-11 00:00:00.000000

Captures every ALTER TABLE that _fix_schema() has been applying at runtime
since migration 0005 was written. After this migration lands, _fix_schema()
becomes a pure no-op on fresh PostgreSQL databases.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _col_exists(bind, table: str, column: str) -> bool:
    r = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name=:t AND column_name=:c"
        ),
        {"t": table, "c": column},
    )
    return r.fetchone() is not None


def _index_exists(bind, name: str) -> bool:
    """Works on PostgreSQL only (pg_indexes). Returns False on SQLite."""
    try:
        r = bind.execute(
            sa.text("SELECT 1 FROM pg_indexes WHERE indexname=:n"),
            {"n": name},
        )
        return r.fetchone() is not None
    except Exception:
        return False


def _col_nullable(bind, table: str, column: str) -> bool:
    """Returns True if the column is currently nullable."""
    r = bind.execute(
        sa.text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name=:t AND column_name=:c"
        ),
        {"t": table, "c": column},
    )
    row = r.fetchone()
    return row is not None and row[0] == "YES"


def _is_pg(bind) -> bool:
    return bind.dialect.name == "postgresql"


# ─────────────────────────────────────────────────────────────────────────────
# upgrade
# ─────────────────────────────────────────────────────────────────────────────

def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_pg(bind)

    # ── users.email → nullable ────────────────────────────────────────────────
    if pg and not _col_nullable(bind, "users", "email"):
        op.alter_column("users", "email", nullable=True)

    # ── users.hashed_password → nullable (Telegram login without password) ───
    if pg and not _col_nullable(bind, "users", "hashed_password"):
        op.alter_column("users", "hashed_password", nullable=True)

    # ── users.phone → nullable + unique index ─────────────────────────────────
    if pg:
        if not _col_nullable(bind, "users", "phone"):
            # fill synthetic values to avoid unique-constraint collisions on NULL
            bind.execute(sa.text(
                "UPDATE users SET phone = '+700000' || LPAD(id::text, 7, '0') "
                "WHERE phone IS NULL OR phone = ''"
            ))
            op.alter_column("users", "phone", nullable=True)

        if not _index_exists(bind, "ix_users_phone"):
            bind.execute(sa.text(
                "CREATE UNIQUE INDEX ix_users_phone ON users(phone) "
                "WHERE phone IS NOT NULL"
            ))

    # ── users.telegram_id → unique index ─────────────────────────────────────
    if pg and not _index_exists(bind, "ix_users_telegram_id"):
        # clear empty strings so they don't block the partial unique index
        bind.execute(sa.text(
            "UPDATE users SET telegram_id = NULL WHERE telegram_id = ''"
        ))
        bind.execute(sa.text(
            "CREATE UNIQUE INDEX ix_users_telegram_id ON users(telegram_id) "
            "WHERE telegram_id IS NOT NULL"
        ))

    # ── users.company_name ────────────────────────────────────────────────────
    if not _col_exists(bind, "users", "company_name"):
        op.add_column("users", sa.Column("company_name", sa.String(150), nullable=True))

    # ── users.last_subscription_reminder ─────────────────────────────────────
    if not _col_exists(bind, "users", "last_subscription_reminder"):
        op.add_column("users", sa.Column("last_subscription_reminder", sa.DateTime(), nullable=True))

    # ── otp_codes.phone ───────────────────────────────────────────────────────
    if not _col_exists(bind, "otp_codes", "phone"):
        op.add_column("otp_codes", sa.Column("phone", sa.String(30), nullable=True))
        # back-fill from email so the column is never empty for existing rows
        bind.execute(sa.text(
            "UPDATE otp_codes SET phone = COALESCE(email, 'unknown') WHERE phone IS NULL"
        ))
        if pg:
            op.alter_column("otp_codes", "phone", nullable=False)

    # ── otp_codes.email → nullable ────────────────────────────────────────────
    if pg and not _col_nullable(bind, "otp_codes", "email"):
        op.alter_column("otp_codes", "email", nullable=True)

    # ── otp_codes.code → VARCHAR(64) ──────────────────────────────────────────
    if pg:
        r = bind.execute(sa.text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_name='otp_codes' AND column_name='code'"
        ))
        row = r.fetchone()
        if row and row[0] is not None and row[0] < 64:
            op.alter_column(
                "otp_codes", "code",
                type_=sa.String(64),
                existing_type=sa.String(row[0]),
            )

    # ── locations: new columns ────────────────────────────────────────────────
    _loc_cols = [
        ("ignore_background_media", sa.Boolean(),    {"server_default": "true",     "nullable": True}),
        ("business_description",    sa.Text(),        {"nullable": True}),
        ("greeting_script",         sa.Text(),        {"nullable": True}),
        ("upsell_script",           sa.Text(),        {"nullable": True}),
        ("track_upsell",            sa.Boolean(),    {"server_default": "true",     "nullable": True}),
        ("track_greeting",          sa.Boolean(),    {"server_default": "true",     "nullable": True}),
        ("track_goodbye",           sa.Boolean(),    {"server_default": "true",     "nullable": True}),
        ("employees",               sa.JSON(),        {"nullable": True}),
    ]
    for col_name, col_type, kwargs in _loc_cols:
        if not _col_exists(bind, "locations", col_name):
            op.add_column("locations", sa.Column(col_name, col_type, **kwargs))

    # ── reports: new columns ──────────────────────────────────────────────────
    _rep_cols = [
        ("employee_name", sa.String(100), {"nullable": True}),
        ("energy_level",  sa.Integer(),   {"nullable": True}),
        ("score",         sa.Integer(),   {"nullable": True}),
        ("s3_key",        sa.Text(),      {"nullable": True}),
    ]
    for col_name, col_type, kwargs in _rep_cols:
        if not _col_exists(bind, "reports", col_name):
            op.add_column("reports", sa.Column(col_name, col_type, **kwargs))


# ─────────────────────────────────────────────────────────────────────────────
# downgrade  (best-effort — drops added columns, leaves nullable changes)
# ─────────────────────────────────────────────────────────────────────────────

def downgrade() -> None:
    bind = op.get_bind()
    pg = _is_pg(bind)

    for col in ("employee_name", "energy_level", "score", "s3_key"):
        if _col_exists(bind, "reports", col):
            op.drop_column("reports", col)

    for col in ("ignore_background_media", "business_description", "greeting_script",
                "upsell_script", "track_upsell", "track_greeting", "track_goodbye", "employees"):
        if _col_exists(bind, "locations", col):
            op.drop_column("locations", col)

    if _col_exists(bind, "users", "last_subscription_reminder"):
        op.drop_column("users", "last_subscription_reminder")

    if _col_exists(bind, "users", "company_name"):
        op.drop_column("users", "company_name")

    if _col_exists(bind, "otp_codes", "phone"):
        op.drop_column("otp_codes", "phone")

    if pg:
        if _index_exists(bind, "ix_users_telegram_id"):
            bind.execute(sa.text("DROP INDEX IF EXISTS ix_users_telegram_id"))
        if _index_exists(bind, "ix_users_phone"):
            bind.execute(sa.text("DROP INDEX IF EXISTS ix_users_phone"))
