"""Initial schema — all tables

Revision ID: 0001
Revises:
Create Date: 2024-01-01 00:00:00.000000

This migration is IDEMPOTENT: every op uses checkfirst=True or
IF NOT EXISTS so it is safe to run against an existing database
that was previously managed via the ALTER TABLE approach.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # ── users ──────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id",              sa.Integer(),     primary_key=True),
        sa.Column("name",            sa.String(100),   nullable=False),
        sa.Column("email",           sa.String(150),   nullable=False, unique=True),
        sa.Column("phone",           sa.String(20)),
        sa.Column("hashed_password", sa.String(255),   nullable=False),
        sa.Column("telegram_id",     sa.String(50)),
        sa.Column("telegram_chat",   sa.String(50)),
        sa.Column("is_verified",     sa.Boolean(),     server_default=sa.false()),
        sa.Column("plan",            sa.String(20),    server_default="trial"),
        sa.Column("plan_expires",    sa.DateTime()),
        sa.Column("is_active",       sa.Boolean(),     server_default=sa.true()),
        sa.Column("is_admin",        sa.Boolean(),     server_default=sa.false()),
        sa.Column("created_at",      sa.DateTime()),
        sa.Column("last_login",      sa.DateTime()),
        if_not_exists=True,
    )
    # ── locations ──────────────────────────────────────────────
    op.create_table(
        "locations",
        sa.Column("id",              sa.Integer(),     primary_key=True),
        sa.Column("owner_id",        sa.Integer(),     sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name",            sa.String(150),   nullable=False),
        sa.Column("business_type",   sa.String(30),    server_default="coffee"),
        sa.Column("address",         sa.String(255)),
        sa.Column("city",            sa.String(100),   server_default="Алматы"),
        sa.Column("telegram_chat",   sa.String(50)),
        sa.Column("vad_level",       sa.Integer(),     server_default="2"),
        sa.Column("silence_seconds", sa.Integer(),     server_default="3"),
        sa.Column("language",        sa.String(10),    server_default="ru"),
        sa.Column("custom_phrases",  sa.JSON()),
        sa.Column("allowed_phones",  sa.JSON()),
        sa.Column("required_upsells",sa.JSON()),
        sa.Column("ignore_internal_profanity", sa.Boolean(), server_default=sa.false()),
        sa.Column("is_active",       sa.Boolean(),     server_default=sa.true()),
        sa.Column("api_key",         sa.String(64),    unique=True),
        sa.Column("created_at",      sa.DateTime()),
        sa.Column("last_seen",       sa.DateTime()),
        sa.Column("last_ping_at",    sa.DateTime()),
        sa.Column("offline_alerted_at", sa.DateTime()),
        if_not_exists=True,
    )

    # ── reports ────────────────────────────────────────────────
    op.create_table(
        "reports",
        sa.Column("id",              sa.Integer(),     primary_key=True),
        sa.Column("location_id",     sa.Integer(),     sa.ForeignKey("locations.id"), nullable=False),
        sa.Column("timestamp",       sa.DateTime(),    index=True),
        sa.Column("transcript",      sa.Text(),        nullable=False),
        sa.Column("duration_sec",    sa.Float()),
        sa.Column("audio_size_kb",   sa.Integer()),
        sa.Column("found_categories",sa.JSON()),
        sa.Column("has_greeting",    sa.Boolean(),     server_default=sa.false(), index=True),
        sa.Column("has_thanks",      sa.Boolean(),     server_default=sa.false()),
        sa.Column("has_goodbye",     sa.Boolean(),     server_default=sa.false()),
        sa.Column("has_bonus",       sa.Boolean(),     server_default=sa.false(), index=True),
        sa.Column("has_bad",         sa.Boolean(),     server_default=sa.false(), index=True),
        sa.Column("has_fraud",       sa.Boolean(),     server_default=sa.false(), index=True),
        sa.Column("tone",            sa.String(20),    server_default="neutral"),
        sa.Column("tone_score",      sa.Float(),       server_default="0.5"),
        sa.Column("gpt_score",       sa.Integer()),
        sa.Column("gpt_summary",     sa.Text()),
        sa.Column("gpt_details",     sa.JSON()),
        sa.Column("speakers",        sa.JSON()),
        sa.Column("shift_number",    sa.Integer()),
        sa.Column("is_priority",     sa.Boolean(),     server_default=sa.false(), index=True),
        sa.Column("audio_sha256",    sa.String(64)),
        sa.Column("s3_url",          sa.Text()),
        sa.Column("payment_confirmed",    sa.Boolean()),
        sa.Column("upsell_attempt",       sa.Boolean()),
        sa.Column("customer_satisfaction",sa.Integer()),
        sa.Column("is_personal_talk",     sa.Boolean(), server_default=sa.false(), index=True),
        sa.Column("is_hidden",            sa.Boolean(), server_default=sa.false(), index=True),
        sa.Column("conversation_context", sa.String(30), server_default="unknown", index=True),
        sa.Column("context_score",        sa.Float()),
        sa.Column("fraud_status",         sa.String(30), server_default="normal", index=True),
        sa.Column("s3_deleted_at",        sa.DateTime()),
        if_not_exists=True,
    )

    # ── alerts ─────────────────────────────────────────────────
    op.create_table(
        "alerts",
        sa.Column("id",           sa.Integer(),  primary_key=True),
        sa.Column("location_id",  sa.Integer(),  sa.ForeignKey("locations.id"), nullable=False),
        sa.Column("report_id",    sa.Integer(),  sa.ForeignKey("reports.id")),
        sa.Column("timestamp",    sa.DateTime(), index=True),
        sa.Column("alert_type",   sa.String(30), nullable=False, index=True),
        sa.Column("severity",     sa.String(10), server_default="high"),
        sa.Column("transcript",   sa.Text()),
        sa.Column("trigger_phrase",sa.String(255)),
        sa.Column("is_resolved",  sa.Boolean(),  server_default=sa.false()),
        sa.Column("resolved_at",  sa.DateTime()),
        sa.Column("resolved_by",  sa.String(100)),
        sa.Column("manager_notes",sa.Text()),
        if_not_exists=True,
    )

    # ── shifts ─────────────────────────────────────────────────
    op.create_table(
        "shifts",
        sa.Column("id",           sa.Integer(),  primary_key=True),
        sa.Column("location_id",  sa.Integer(),  sa.ForeignKey("locations.id"), nullable=False),
        sa.Column("shift_number", sa.Integer(),  nullable=False),
        sa.Column("date",         sa.DateTime(), nullable=False),
        sa.Column("total_reports",sa.Integer(),  server_default="0"),
        sa.Column("alerts_count", sa.Integer(),  server_default="0"),
        sa.Column("avg_score",    sa.Float()),
        sa.Column("created_at",   sa.DateTime()),
        if_not_exists=True,
    )

    # ── payments ───────────────────────────────────────────────
    op.create_table(
        "payments",
        sa.Column("id",          sa.Integer(),  primary_key=True),
        sa.Column("user_id",     sa.Integer(),  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount",      sa.Float(),    nullable=False),
        sa.Column("currency",    sa.String(10), server_default="KZT"),
        sa.Column("plan",        sa.String(20)),
        sa.Column("status",      sa.String(20), server_default="pending"),
        sa.Column("kaspi_ref",   sa.String(100)),
        sa.Column("created_at",  sa.DateTime()),
        sa.Column("paid_at",     sa.DateTime()),
        if_not_exists=True,
    )

    # ── pos_transactions ───────────────────────────────────────
    op.create_table(
        "pos_transactions",
        sa.Column("id",               sa.Integer(),  primary_key=True),
        sa.Column("location_id",      sa.Integer(),  sa.ForeignKey("locations.id"), nullable=False, index=True),
        sa.Column("timestamp",        sa.DateTime(), nullable=False, index=True),
        sa.Column("amount",           sa.Float(),    nullable=False),
        sa.Column("receipt_id",       sa.String(100)),
        sa.Column("currency",         sa.String(10), server_default="KZT"),
        sa.Column("cashier_id",       sa.String(100)),
        sa.Column("raw_data",         sa.Text()),
        sa.Column("pos_type",         sa.String(20), server_default="none"),
        sa.Column("items",            sa.JSON()),
        sa.Column("is_matched",       sa.Boolean(),  server_default=sa.false(), index=True),
        sa.Column("matched_report_id",sa.Integer(),  sa.ForeignKey("reports.id")),
        sa.Column("created_at",       sa.DateTime()),
        if_not_exists=True,
    )

    # ── failed_jobs ────────────────────────────────────────────
    op.create_table(
        "failed_jobs",
        sa.Column("id",              sa.Integer(),  primary_key=True),
        sa.Column("location_id",     sa.Integer(),  nullable=False, index=True),
        sa.Column("audio_path",      sa.String(500)),
        sa.Column("transcript_text", sa.Text()),
        sa.Column("language",        sa.String(10)),
        sa.Column("audio_size_kb",   sa.Integer(),  server_default="0"),
        sa.Column("business_type",   sa.String(50)),
        sa.Column("custom_phrases",  sa.JSON()),
        sa.Column("telegram_chat",   sa.String(100)),
        sa.Column("location_name",   sa.String(200)),
        sa.Column("retry_count",     sa.Integer(),  server_default="0"),
        sa.Column("next_retry_at",   sa.DateTime(), nullable=False, index=True),
        sa.Column("last_error",      sa.Text()),
        sa.Column("created_at",      sa.DateTime()),
        sa.Column("status",          sa.String(20), server_default="pending", index=True),
        if_not_exists=True,
    )

    # ── incidents ──────────────────────────────────────────────
    op.create_table(
        "incidents",
        sa.Column("id",             sa.Integer(),   primary_key=True),
        sa.Column("location_id",    sa.Integer(),   sa.ForeignKey("locations.id"), nullable=False, index=True),
        sa.Column("report_id",      sa.Integer(),   sa.ForeignKey("reports.id")),
        sa.Column("incident_type",  sa.String(30),  nullable=False, index=True),
        sa.Column("severity",       sa.String(20),  server_default="high"),
        sa.Column("description",    sa.Text()),
        sa.Column("proof_s3_url",   sa.Text()),
        sa.Column("proof_sha256",   sa.String(64)),
        sa.Column("detected_phone", sa.String(30)),
        sa.Column("upsell_phrase",  sa.String(300)),
        sa.Column("missing_item",   sa.String(300)),
        sa.Column("status",         sa.String(20),  server_default="open", index=True),
        sa.Column("resolved_at",    sa.DateTime()),
        sa.Column("created_at",     sa.DateTime(),  index=True),
        if_not_exists=True,
    )

    # ── otp_codes ──────────────────────────────────────────────
    op.create_table(
        "otp_codes",
        sa.Column("id",         sa.Integer(),  primary_key=True),
        sa.Column("email",      sa.String(255), nullable=False, index=True),
        sa.Column("code",       sa.String(6),   nullable=False),
        sa.Column("expires_at", sa.DateTime(),  nullable=False),
        sa.Column("used",       sa.Boolean(),   server_default=sa.false()),
        sa.Column("created_at", sa.DateTime()),
        if_not_exists=True,
    )


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_table("otp_codes")
    op.drop_table("incidents")
    op.drop_table("failed_jobs")
    op.drop_table("pos_transactions")
    op.drop_table("payments")
    op.drop_table("shifts")
    op.drop_table("alerts")
    op.drop_table("reports")
    op.drop_table("locations")
    op.drop_table("users")
