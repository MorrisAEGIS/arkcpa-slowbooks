"""Explicit research grants and immutable authority capture/review evidence.

No source seeding, permission grants, rights approvals, or legacy promotion.
"""

from alembic import op
import sqlalchemy as sa

revision = "aa1b2c3d4e5f"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None

IMMUTABLE = (
    "ark_source_snapshots",
    "ark_authority_retrievals",
    "ark_authority_reviews",
    "ark_authority_passages",
)


def upgrade():
    for column in (
        sa.Column(
            "capture_rights", sa.String(30), nullable=False, server_default="unreviewed"
        ),
        sa.Column(
            "allow_model_context",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("rights_basis", sa.Text(), nullable=True),
        sa.Column("rights_reviewed_by", sa.String(100), nullable=True),
        sa.Column("rights_valid_until", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("ark_authority_sources", column)
    with op.batch_alter_table("ark_authority_sources") as batch:
        batch.create_check_constraint(
            "ck_ark_authority_capture_rights",
            "capture_rights IN ('unreviewed', 'public_domain', 'licensed', 'prohibited')",
        )
    op.add_column(
        "ark_source_snapshots", sa.Column("content", sa.LargeBinary(), nullable=True)
    )
    op.add_column(
        "ark_source_snapshots", sa.Column("extracted_text", sa.Text(), nullable=True)
    )
    op.create_table(
        "ark_research_permissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("permission", sa.String(20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("granted_by", sa.String(100), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "permission", name="uq_ark_research_permission"),
        sa.CheckConstraint(
            "permission IN ('refresh', 'review')", name="ck_ark_research_permission"
        ),
    )
    op.create_table(
        "ark_authority_retrievals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("attempt_id", sa.String(36), nullable=False, index=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("ark_authority_sources.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "snapshot_id",
            sa.Integer(),
            sa.ForeignKey("ark_source_snapshots.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("event", sa.String(20), nullable=False),
        sa.Column("requested_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_definition", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "attempt_id", "event", name="uq_ark_authority_retrieval_event"
        ),
        sa.CheckConstraint(
            "event IN ('started', 'succeeded', 'failed')",
            name="ck_ark_authority_retrieval_event",
        ),
    )
    op.create_index(
        "uq_ark_authority_retrieval_terminal",
        "ark_authority_retrievals",
        ["attempt_id"],
        unique=True,
        postgresql_where=sa.text("event <> 'started'"),
        sqlite_where=sa.text("event <> 'started'"),
    )
    op.create_table(
        "ark_authority_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.Integer(),
            sa.ForeignKey("ark_source_snapshots.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("reviewed_by", sa.String(100), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applicability", sa.JSON(), nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected', 'revoked')",
            name="ck_ark_authority_review_decision",
        ),
    )
    op.create_table(
        "ark_authority_passages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "review_id",
            sa.Integer(),
            sa.ForeignKey("ark_authority_reviews.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("locator", sa.String(300), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """CREATE FUNCTION ark_authority_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'Authority evidence is append-only'; END $$"""
        )
        for table in IMMUTABLE:
            op.execute(
                f"CREATE TRIGGER immutable_authority BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION ark_authority_immutable()"
            )
            op.execute(
                f"CREATE TRIGGER immutable_authority_truncate BEFORE TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION ark_authority_immutable()"
            )
    elif op.get_bind().dialect.name == "sqlite":
        conflict_keys = {
            "ark_source_snapshots": "id = NEW.id OR (source_id = NEW.source_id AND content_hash = NEW.content_hash)",
            "ark_authority_retrievals": "id = NEW.id OR (attempt_id = NEW.attempt_id AND (event = NEW.event OR (event <> 'started' AND NEW.event <> 'started')))",
            "ark_authority_reviews": "id = NEW.id",
            "ark_authority_passages": "id = NEW.id",
        }
        for table in IMMUTABLE:
            # SQLite REPLACE does not fire DELETE triggers unless recursive_triggers
            # is enabled. Guard every PK/unique collision before insertion instead.
            op.execute(
                f"CREATE TRIGGER immutable_{table}_insert BEFORE INSERT ON {table} "
                f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {conflict_keys[table]}) "
                "BEGIN SELECT RAISE(ABORT, 'Authority evidence is append-only'); END"
            )
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'Authority evidence is append-only'); END"
                )
    else:
        raise RuntimeError(
            "Authority evidence requires PostgreSQL or SQLite immutable triggers"
        )


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        for table in IMMUTABLE:
            op.execute(f"DROP TRIGGER immutable_authority ON {table}")
            op.execute(f"DROP TRIGGER immutable_authority_truncate ON {table}")
        op.execute("DROP FUNCTION ark_authority_immutable()")
    elif op.get_bind().dialect.name == "sqlite":
        for table in IMMUTABLE:
            for action in ("insert", "update", "delete"):
                op.execute(f"DROP TRIGGER immutable_{table}_{action}")
    for table in reversed(IMMUTABLE[1:]):
        op.drop_table(table)
    op.drop_table("ark_research_permissions")
    op.drop_column("ark_source_snapshots", "extracted_text")
    op.drop_column("ark_source_snapshots", "content")
    with op.batch_alter_table("ark_authority_sources") as batch:
        batch.drop_constraint("ck_ark_authority_capture_rights", type_="check")
        for name in (
            "capture_rights",
            "allow_model_context",
            "rights_basis",
            "rights_reviewed_by",
            "rights_valid_until",
        ):
            batch.drop_column(name)
