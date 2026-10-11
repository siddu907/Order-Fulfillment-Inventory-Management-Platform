"""Add user token version for immediate access-token revocation.

Revision ID: d8f2a17c9b40
Revises: c4b6de038d12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d8f2a17c9b40"
down_revision: Union[str, None] = "c4b6de038d12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("users", "token_version", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "token_version")
