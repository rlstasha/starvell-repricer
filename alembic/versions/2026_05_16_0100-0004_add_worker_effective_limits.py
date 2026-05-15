"""add worker effective request limits

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-16 01:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "worker_heartbeats",
        sa.Column("effective_request_limit_per_minute", sa.Integer(), nullable=True),
    )
    op.add_column(
        "worker_heartbeats",
        sa.Column("backoff_active", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "worker_heartbeats",
        sa.Column("last_429_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE worker_heartbeats
        SET effective_request_limit_per_minute = request_limit_per_minute
        WHERE effective_request_limit_per_minute IS NULL
        """
    )
    op.alter_column(
        "worker_heartbeats",
        "effective_request_limit_per_minute",
        existing_type=sa.Integer(),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("worker_heartbeats", "last_429_at")
    op.drop_column("worker_heartbeats", "backoff_active")
    op.drop_column("worker_heartbeats", "effective_request_limit_per_minute")
