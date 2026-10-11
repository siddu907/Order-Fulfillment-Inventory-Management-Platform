"""Add Stripe payment state, notification outbox and immutable ledgers.

Revision ID: c4b6de038d12
Revises: a35dd897155f
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c4b6de038d12"
down_revision: Union[str, None] = "a35dd897155f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION reject_immutable_ledger_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'table % is append-only; % is not permitted', TG_TABLE_NAME, TG_OP
                    USING ERRCODE = '55000';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        for table in ("inventory_transactions", "audit_logs"):
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_immutable_ledger_mutation()
                """
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in ("inventory_transactions", "audit_logs"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
        op.execute("DROP FUNCTION IF EXISTS reject_immutable_ledger_mutation()")
    op.drop_table("stripe_webhook_events")
    op.drop_index("ix_outbox_messages_available_at", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_status", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_topic", table_name="outbox_messages")
    op.drop_table("outbox_messages")
    op.drop_constraint("uq_orders_stripe_payment_intent_id", "orders", type_="unique")
    op.drop_column("orders", "stripe_payment_intent_id")
