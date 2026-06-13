"""User referral program: referral_code + referred_by

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-13 00:00:00.000000

Adds two columns to users:
  - referral_code: личный код владельца для приглашений (уникальный, ?ref=CODE)
  - referred_by:   id пользователя, по чьему коду пришёл этот клиент

Награда за приглашение начисляется вручную — здесь только учёт привязки.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
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


def _index_exists(bind, name: str) -> bool:
    try:
        r = bind.execute(
            sa.text("SELECT 1 FROM pg_indexes WHERE indexname=:n"),
            {"n": name},
        )
        return r.fetchone() is not None
    except Exception:
        return False


def _is_pg(bind) -> bool:
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_pg(bind)

    if not _col_exists(bind, "users", "referral_code"):
        op.add_column("users", sa.Column("referral_code", sa.String(12), nullable=True))
    if not _col_exists(bind, "users", "referred_by"):
        op.add_column("users", sa.Column("referred_by", sa.Integer(), nullable=True))

    # Уникальный индекс на referral_code (частичный на PG — несколько NULL разрешены).
    if pg:
        if not _index_exists(bind, "ix_users_referral_code"):
            bind.execute(sa.text(
                "CREATE UNIQUE INDEX ix_users_referral_code ON users(referral_code) "
                "WHERE referral_code IS NOT NULL"
            ))
        if not _index_exists(bind, "ix_users_referred_by"):
            bind.execute(sa.text(
                "CREATE INDEX ix_users_referred_by ON users(referred_by)"
            ))
    else:
        # SQLite (dev): обычные индексы — несколько NULL допускаются по стандарту.
        op.create_index("ix_users_referral_code", "users", ["referral_code"], unique=True)
        op.create_index("ix_users_referred_by", "users", ["referred_by"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()

    if _index_exists(bind, "ix_users_referred_by"):
        bind.execute(sa.text("DROP INDEX IF EXISTS ix_users_referred_by"))
    if _index_exists(bind, "ix_users_referral_code"):
        bind.execute(sa.text("DROP INDEX IF EXISTS ix_users_referral_code"))

    if _col_exists(bind, "users", "referred_by"):
        op.drop_column("users", "referred_by")
    if _col_exists(bind, "users", "referral_code"):
        op.drop_column("users", "referral_code")
