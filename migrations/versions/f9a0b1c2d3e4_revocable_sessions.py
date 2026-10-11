"""Add per-user revocation versions; pre-upgrade cookies require fresh login."""

from alembic import op
import sqlalchemy as sa

revision = "f9a0b1c2d3e4"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("session_version", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade():
    op.drop_column("users", "session_version")
