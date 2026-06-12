"""add machines.agent_token_encrypted

Revision ID: c3a5b2e8f1d4
Revises: b2f4a1c7d9e3
Create Date: 2026-06-10 00:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3a5b2e8f1d4"
down_revision: str | None = "b2f4a1c7d9e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("machines") as batch_op:
        batch_op.add_column(sa.Column("agent_token_encrypted", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("machines") as batch_op:
        batch_op.drop_column("agent_token_encrypted")
