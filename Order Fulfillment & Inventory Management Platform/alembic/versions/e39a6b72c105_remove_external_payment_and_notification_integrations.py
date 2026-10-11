"""Remove external payment and notification integration schema.

Revision ID: e39a6b72c105
Revises: d8f2a17c9b40
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e39a6b72c105"
down_revision: Union[str, None] = "d8f2a17c9b40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("stripe_webhook_events")
    op.drop_table("outbox_messages")
    op.drop_constraint("uq_orders_stripe_payment_intent_id", "orders", type_="unique")
    op.drop_column("orders", "stripe_payment_intent_id")


def downgrade() -> None:
    op.add_column("orders", sa.Column("stripe_payment_intent_id", sa.String(length=255), nullable=True))
    op.create_unique_constraint(
        "uq_orders_stripe_payment_intent_id",
        "orders",
        ["stripe_payment_intent_id"],
    )
    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("topic", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_messages_topic", "outbox_messages", ["topic"])
    op.create_index("ix_outbox_messages_status", "outbox_messages", ["status"])
    op.create_index("ix_outbox_messages_available_at", "outbox_messages", ["available_at"])
    op.create_table(
        "stripe_webhook_events",
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )
