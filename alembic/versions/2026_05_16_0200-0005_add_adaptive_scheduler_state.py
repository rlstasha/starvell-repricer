"""add adaptive scheduler state

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-16 02:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "worker_heartbeats",
        sa.Column("profile_request_usage_per_minute", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "worker_heartbeats",
        sa.Column("account_effective_limit_per_minute", sa.Integer(), nullable=True),
    )
    op.add_column(
        "worker_heartbeats",
        sa.Column("account_request_usage_per_minute", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "worker_heartbeats",
        sa.Column("account_backoff_active", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "worker_heartbeats",
        sa.Column("account_last_429_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "worker_heartbeats",
        sa.Column("account_retry_after_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "worker_heartbeats",
        sa.Column("current_delay_seconds", sa.Float(), nullable=True),
    )
    op.add_column(
        "worker_heartbeats",
        sa.Column("interval_min_seconds", sa.Float(), nullable=True),
    )
    op.add_column(
        "worker_heartbeats",
        sa.Column("interval_max_seconds", sa.Float(), nullable=True),
    )
    op.add_column(
        "worker_heartbeats",
        sa.Column("most_active_position_amount", sa.Integer(), nullable=True),
    )

    op.create_table(
        "position_schedule_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("position_id", sa.Integer(), sa.ForeignKey("positions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position_amount", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.String(length=64), nullable=True),
        sa.Column("proxy_profile", sa.String(length=32), nullable=False),
        sa.Column("base_interval_seconds", sa.Float(), nullable=False),
        sa.Column("current_interval_seconds", sa.Float(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_competitor_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("last_own_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("change_score", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("error_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("last_429_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("position_id", name="uq_position_schedule_state_position"),
    )
    op.create_index("ix_position_schedule_state_position_amount", "position_schedule_state", ["position_amount"])
    op.create_index("ix_position_schedule_state_proxy_profile", "position_schedule_state", ["proxy_profile"])


def downgrade() -> None:
    op.drop_index("ix_position_schedule_state_proxy_profile", table_name="position_schedule_state")
    op.drop_index("ix_position_schedule_state_position_amount", table_name="position_schedule_state")
    op.drop_table("position_schedule_state")
    op.drop_column("worker_heartbeats", "most_active_position_amount")
    op.drop_column("worker_heartbeats", "interval_max_seconds")
    op.drop_column("worker_heartbeats", "interval_min_seconds")
    op.drop_column("worker_heartbeats", "current_delay_seconds")
    op.drop_column("worker_heartbeats", "account_retry_after_until")
    op.drop_column("worker_heartbeats", "account_last_429_at")
    op.drop_column("worker_heartbeats", "account_backoff_active")
    op.drop_column("worker_heartbeats", "account_request_usage_per_minute")
    op.drop_column("worker_heartbeats", "account_effective_limit_per_minute")
    op.drop_column("worker_heartbeats", "profile_request_usage_per_minute")
