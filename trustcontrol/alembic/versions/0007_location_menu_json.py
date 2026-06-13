"""Add locations.menu_json for structured menu storage

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-11 00:00:00.000000

Structured menu JSON: [{"name": "Капучино", "variants": ["S","M","L"], "price": 800}]
Used in transcription glossary (flat names) and future upsell analysis (Block 4).
custom_phrases repurposed as flat STT glossary — no schema change needed there.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
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
    if not _col_exists(bind, "locations", "menu_json"):
        op.add_column("locations", sa.Column("menu_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _col_exists(bind, "locations", "menu_json"):
        op.drop_column("locations", "menu_json")
