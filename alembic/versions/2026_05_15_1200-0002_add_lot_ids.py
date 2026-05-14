"""add lot ids

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-15 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


LOT_IDS_BY_AMOUNT = {
    80: "1996",
    200: "1998",
    400: "1999",
    500: "2000",
    800: "2002",
    1000: "2003",
    1200: "2004",
    1700: "2005",
    2000: "2006",
    2100: "2007",
    2500: "2008",
    3600: "2009",
    4500: "2010",
    10000: "2011",
    22500: "2012",
}


def upgrade() -> None:
    op.add_column("positions", sa.Column("lot_id", sa.String(length=64), nullable=True))

    positions = sa.table(
        "positions",
        sa.column("robux_amount", sa.Integer()),
        sa.column("lot_id", sa.String(length=64)),
    )
    for amount, lot_id in LOT_IDS_BY_AMOUNT.items():
        op.execute(
            positions.update()
            .where(positions.c.robux_amount == amount)
            .values(lot_id=lot_id)
        )


def downgrade() -> None:
    op.drop_column("positions", "lot_id")
