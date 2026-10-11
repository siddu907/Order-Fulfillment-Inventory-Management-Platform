"""Add configurable order tax rate and tax-rate snapshots.

Revision ID: f314a8c61d2b
Revises: e39a6b72c105
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f314a8c61d2b"
down_revision: Union[str, None] = "e39a6b72c105"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tax_configuration",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rate_percent", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_tax_configuration_singleton"),
        sa.CheckConstraint(
            "rate_percent >= 0 AND rate_percent <= 100",
            name="ck_tax_rate_percent_range",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    configuration_table = sa.table(
        "tax_configuration",
        sa.column("id", sa.Integer()),
        sa.column("rate_percent", sa.Numeric(5, 2)),
    )
    op.bulk_insert(configuration_table, [{"id": 1, "rate_percent": 0}])
    op.add_column(
        "order_items",
        sa.Column("tax_rate", sa.Numeric(5, 2), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("order_items", "tax_rate")
    op.drop_table("tax_configuration")
