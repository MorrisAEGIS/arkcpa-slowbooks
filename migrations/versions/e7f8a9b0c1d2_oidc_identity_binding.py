"""bind local users to immutable OIDC identities

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-09-09
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, None] = "d6e7f8a9b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("oidc_issuer", sa.String(length=500), nullable=True))
        batch.add_column(sa.Column("oidc_subject", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("email", sa.String(length=320), nullable=True))
        batch.create_index("ix_users_email", ["email"], unique=False)
        batch.create_unique_constraint(
            "uq_users_oidc_identity", ["oidc_issuer", "oidc_subject"]
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("uq_users_oidc_identity", type_="unique")
        batch.drop_index("ix_users_email")
        batch.drop_column("email")
        batch.drop_column("oidc_subject")
        batch.drop_column("oidc_issuer")
