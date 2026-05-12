"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-12 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("robux_amount", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("strategy", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("robux_amount"),
    )
    op.create_index(op.f("ix_positions_robux_amount"), "positions", ["robux_amount"])

    op.create_table(
        "position_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("min_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("max_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("step", sa.Numeric(12, 2), nullable=False),
        sa.Column("min_rating", sa.Numeric(3, 2), nullable=False),
        sa.Column("ignore_no_rating", sa.Boolean(), nullable=False),
        sa.Column("fallback_behavior", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("position_id"),
    )

    op.create_table(
        "position_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("last_seen_competitor_price", sa.Numeric(12, 2)),
        sa.Column("current_own_price", sa.Numeric(12, 2)),
        sa.Column("calculated_price", sa.Numeric(12, 2)),
        sa.Column("last_update_time", sa.DateTime(timezone=True)),
        sa.Column("last_success_time", sa.DateTime(timezone=True)),
        sa.Column("error_status", sa.String(length=64)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("position_id"),
    )

    op.create_table(
        "competitor_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("seller_id", sa.String(length=128)),
        sa.Column("seller_username", sa.String(length=255)),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column("rating", sa.Numeric(3, 2)),
        sa.Column("has_rating", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean()),
        sa.Column("is_ignored", sa.Boolean(), nullable=False),
        sa.Column("ignore_reason", sa.String(length=255)),
        sa.Column("raw_payload", sa.JSON()),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="CASCADE"),
    )
    op.create_index(
        op.f("ix_competitor_snapshots_position_id"), "competitor_snapshots", ["position_id"]
    )
    op.create_index(op.f("ix_competitor_snapshots_seen_at"), "competitor_snapshots", ["seen_at"])

    op.create_table(
        "price_update_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("old_price", sa.Numeric(12, 2)),
        sa.Column("new_price", sa.Numeric(12, 2)),
        sa.Column("competitor_price", sa.Numeric(12, 2)),
        sa.Column("competitor_seller_id", sa.String(length=128)),
        sa.Column("competitor_seller_username", sa.String(length=255)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="CASCADE"),
    )
    op.create_index(op.f("ix_price_update_logs_position_id"), "price_update_logs", ["position_id"])
    op.create_index(op.f("ix_price_update_logs_created_at"), "price_update_logs", ["created_at"])

    op.create_table(
        "api_request_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer()),
        sa.Column("request_type", sa.String(length=64), nullable=False),
        sa.Column("position_id", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="SET NULL"),
    )
    op.create_index(op.f("ix_api_request_logs_created_at"), "api_request_logs", ["created_at"])

    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("key", name="uq_app_settings_key"),
    )

    op.create_table(
        "worker_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("last_cycle_at", sa.DateTime(timezone=True)),
        sa.Column("last_position_amount", sa.Integer()),
        sa.Column("last_status", sa.String(length=32)),
        sa.Column("last_error", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", name="uq_worker_state_name"),
    )

    op.create_table(
        "bot_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=128), nullable=False),
        sa.Column("payload_json", sa.JSON()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("telegram_user_id", name="uq_bot_state_telegram_user_id"),
    )


def downgrade() -> None:
    op.drop_table("bot_state")
    op.drop_table("worker_state")
    op.drop_table("app_settings")
    op.drop_index(op.f("ix_api_request_logs_created_at"), table_name="api_request_logs")
    op.drop_table("api_request_logs")
    op.drop_index(op.f("ix_price_update_logs_created_at"), table_name="price_update_logs")
    op.drop_index(op.f("ix_price_update_logs_position_id"), table_name="price_update_logs")
    op.drop_table("price_update_logs")
    op.drop_index(op.f("ix_competitor_snapshots_seen_at"), table_name="competitor_snapshots")
    op.drop_index(op.f("ix_competitor_snapshots_position_id"), table_name="competitor_snapshots")
    op.drop_table("competitor_snapshots")
    op.drop_table("position_state")
    op.drop_table("position_settings")
    op.drop_index(op.f("ix_positions_robux_amount"), table_name="positions")
    op.drop_table("positions")
