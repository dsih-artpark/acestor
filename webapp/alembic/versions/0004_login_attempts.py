"""login attempts

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-08

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("ip", postgresql.INET(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_login_attempts_ip_at", "login_attempts", ["ip", sa.text("at DESC")]
    )
    op.create_index(
        "ix_login_attempts_email_at", "login_attempts", ["email", sa.text("at DESC")]
    )


def downgrade() -> None:
    op.drop_index("ix_login_attempts_email_at", table_name="login_attempts")
    op.drop_index("ix_login_attempts_ip_at", table_name="login_attempts")
    op.drop_table("login_attempts")
