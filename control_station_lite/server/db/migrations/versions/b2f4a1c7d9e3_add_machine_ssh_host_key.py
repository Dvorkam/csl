"""add machines.ssh_host_key

Revision ID: b2f4a1c7d9e3
Revises: ce55c137920e
Create Date: 2026-06-10 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2f4a1c7d9e3"
down_revision: str | None = "ce55c137920e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("machines") as batch_op:
        batch_op.add_column(sa.Column("ssh_host_key", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("machines") as batch_op:
        batch_op.drop_column("ssh_host_key")
