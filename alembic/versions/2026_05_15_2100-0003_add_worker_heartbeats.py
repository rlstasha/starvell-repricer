"""add worker heartbeats

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-15 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("worker_group", sa.String(length=32), nullable=False),
        sa.Column("hostname", sa.String(length=255)),
        sa.Column("public_ip", sa.String(length=64)),
        sa.Column("assigned_positions", sa.JSON(), nullable=False),
        sa.Column("request_limit_per_minute", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("errors_429", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors_403", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors_timeout", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consecutive_errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("safe_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("worker_group", name="uq_worker_heartbeats_group"),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
