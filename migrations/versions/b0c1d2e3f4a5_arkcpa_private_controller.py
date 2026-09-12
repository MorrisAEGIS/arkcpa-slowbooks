"""add Ark CPA private Controller governance and provenance

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-09-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b0c1d2e3f4a5"
down_revision: Union[str, None] = "a9b0c1d2e3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("ark_entities") as batch:
        batch.add_column(
            sa.Column(
                "posting_mode",
                sa.String(length=30),
                nullable=False,
                server_default="draft_only",
            )
        )
        batch.add_column(
            sa.Column(
                "facts_status",
                sa.String(length=20),
                nullable=False,
                server_default="incomplete",
            )
        )
        batch.add_column(
            sa.Column("profile", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
        )
        batch.create_check_constraint(
            "ck_ark_entities_posting_mode",
            "posting_mode IN ('draft_only', 'assisted', 'autopost_ordinary', 'frozen')",
        )
        batch.create_check_constraint(
            "ck_ark_entities_facts_status",
            "facts_status IN ('incomplete', 'verified', 'hold')",
        )
        batch.create_index("ix_ark_entities_posting_mode", ["posting_mode"])
        batch.create_index("ix_ark_entities_facts_status", ["facts_status"])

    with op.batch_alter_table("ark_entity_access") as batch:
        batch.add_column(
            sa.Column(
                "protected_approver",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    with op.batch_alter_table("ark_protected_actions") as batch:
        batch.add_column(sa.Column("approval_note", sa.String(length=1000), nullable=True))
        batch.add_column(sa.Column("decision_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "ark_entity_relationships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "entity_id",
            sa.Integer(),
            sa.ForeignKey("ark_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "related_entity_id",
            sa.Integer(),
            sa.ForeignKey("ark_entities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("subject_label", sa.String(length=240), nullable=False),
        sa.Column("relationship_type", sa.String(length=80), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "source_evidence_id",
            sa.Integer(),
            sa.ForeignKey("ark_evidence.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'historical', 'unverified')",
            name="ck_ark_entity_relationship_status",
        ),
    )
    op.create_index(
        "ix_ark_entity_relationships_entity_id", "ark_entity_relationships", ["entity_id"]
    )
    op.create_index(
        "ix_ark_entity_relationships_relationship_type",
        "ark_entity_relationships",
        ["relationship_type"],
    )

    op.create_table(
        "ark_import_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "entity_id",
            sa.Integer(),
            sa.ForeignKey("ark_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('ark_files', 'bank_file', 'mail', 'tax_source')",
            name="ck_ark_import_run_kind",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'blocked', 'failed')",
            name="ck_ark_import_run_status",
        ),
    )
    op.create_index("ix_ark_import_runs_entity_id", "ark_import_runs", ["entity_id"])
    op.create_index("ix_ark_import_runs_kind", "ark_import_runs", ["kind"])
    op.create_index("ix_ark_import_runs_status", "ark_import_runs", ["status"])

    op.create_table(
        "ark_evidence_facts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "evidence_id",
            sa.Integer(),
            sa.ForeignKey("ark_evidence.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fact_key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("locator", sa.String(length=240), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("extractor_version", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reviewed_by", sa.String(length=100), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('extracted', 'verified', 'rejected')",
            name="ck_ark_evidence_fact_status",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_ark_evidence_fact_confidence",
        ),
        sa.UniqueConstraint(
            "evidence_id", "fact_key", "locator", name="uq_ark_evidence_fact"
        ),
    )
    op.create_index("ix_ark_evidence_facts_evidence_id", "ark_evidence_facts", ["evidence_id"])
    op.create_index("ix_ark_evidence_facts_fact_key", "ark_evidence_facts", ["fact_key"])
    op.create_index("ix_ark_evidence_facts_status", "ark_evidence_facts", ["status"])

    op.create_table(
        "ark_authority_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("jurisdiction", sa.String(length=80), nullable=False),
        sa.Column("authority_level", sa.String(length=30), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("license_note", sa.String(length=300), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "authority_level IN ('law', 'regulation', 'court', 'administrative', "
            "'proposed', 'lead')",
            name="ck_ark_authority_source_level",
        ),
        sa.UniqueConstraint("code", name="uq_ark_authority_source_code"),
    )
    op.create_index(
        "ix_ark_authority_sources_jurisdiction", "ark_authority_sources", ["jurisdiction"]
    )
    op.create_index(
        "ix_ark_authority_sources_authority_level",
        "ark_authority_sources",
        ["authority_level"],
    )

    op.create_table(
        "ark_source_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("ark_authority_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("snapshot_metadata", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "status IN ('current', 'superseded', 'proposed', 'failed')",
            name="ck_ark_source_snapshot_status",
        ),
        sa.UniqueConstraint("source_id", "content_hash", name="uq_ark_source_snapshot_hash"),
    )
    op.create_index("ix_ark_source_snapshots_source_id", "ark_source_snapshots", ["source_id"])
    op.create_index("ix_ark_source_snapshots_content_hash", "ark_source_snapshots", ["content_hash"])
    op.create_index("ix_ark_source_snapshots_status", "ark_source_snapshots", ["status"])

    op.create_table(
        "ark_rule_proposals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_snapshot_id",
            sa.Integer(),
            sa.ForeignKey("ark_source_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("proposed_change", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("test_results", sa.JSON(), nullable=False),
        sa.Column("requested_by", sa.String(length=100), nullable=False),
        sa.Column("approved_by", sa.String(length=100), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'testing', 'approved', 'rejected', 'promoted')",
            name="ck_ark_rule_proposal_status",
        ),
        sa.UniqueConstraint("source_snapshot_id", "code", name="uq_ark_rule_proposal"),
    )
    op.create_index(
        "ix_ark_rule_proposals_source_snapshot_id",
        "ark_rule_proposals",
        ["source_snapshot_id"],
    )
    op.create_index("ix_ark_rule_proposals_status", "ark_rule_proposals", ["status"])

    op.create_table(
        "ark_controller_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "entity_id",
            sa.Integer(),
            sa.ForeignKey("ark_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "run_type IN ('daily', 'weekly', 'monthly', 'annual', 'candidate')",
            name="ck_ark_controller_run_type",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'blocked', 'failed')",
            name="ck_ark_controller_run_status",
        ),
    )
    op.create_index("ix_ark_controller_runs_entity_id", "ark_controller_runs", ["entity_id"])
    op.create_index("ix_ark_controller_runs_run_type", "ark_controller_runs", ["run_type"])
    op.create_index("ix_ark_controller_runs_status", "ark_controller_runs", ["status"])

    op.create_table(
        "ark_controller_issues",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("ark_controller_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "entity_id",
            sa.Integer(),
            sa.ForeignKey("ark_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "evidence_id",
            sa.Integer(),
            sa.ForeignKey("ark_evidence.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("category", sa.String(length=80), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("assigned_to", sa.String(length=100), nullable=True),
        sa.Column("resolved_by", sa.String(length=100), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "severity IN ('info', 'low', 'medium', 'high', 'critical')",
            name="ck_ark_controller_issue_severity",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'acknowledged', 'resolved', 'dismissed')",
            name="ck_ark_controller_issue_status",
        ),
    )
    op.create_index("ix_ark_controller_issues_run_id", "ark_controller_issues", ["run_id"])
    op.create_index("ix_ark_controller_issues_entity_id", "ark_controller_issues", ["entity_id"])
    op.create_index("ix_ark_controller_issues_category", "ark_controller_issues", ["category"])
    op.create_index("ix_ark_controller_issues_severity", "ark_controller_issues", ["severity"])
    op.create_index("ix_ark_controller_issues_status", "ark_controller_issues", ["status"])

    op.create_table(
        "ark_agent_decision_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.Integer(),
            sa.ForeignKey("ark_posting_candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "controller_run_id",
            sa.Integer(),
            sa.ForeignKey("ark_controller_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("agent_role", sa.String(length=20), nullable=False),
        sa.Column("model_id", sa.String(length=160), nullable=False),
        sa.Column("model_family", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("context_hash", sa.String(length=64), nullable=False),
        sa.Column("rationale_hash", sa.String(length=64), nullable=False),
        sa.Column("response_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "agent_role IN ('bookkeeper', 'controller')",
            name="ck_ark_agent_decision_run_role",
        ),
        sa.CheckConstraint(
            "decision IN ('approve', 'reject', 'escalate')",
            name="ck_ark_agent_decision_run_value",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_ark_agent_decision_run_confidence",
        ),
    )
    op.create_index(
        "ix_ark_agent_decision_runs_candidate_id", "ark_agent_decision_runs", ["candidate_id"]
    )
    op.create_index(
        "ix_ark_agent_decision_runs_controller_run_id",
        "ark_agent_decision_runs",
        ["controller_run_id"],
    )
    op.create_index(
        "ix_ark_agent_decision_runs_created_at", "ark_agent_decision_runs", ["created_at"]
    )

    op.create_table(
        "ark_workpaper_packages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "entity_id",
            sa.Integer(),
            sa.ForeignKey("ark_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "obligation_id",
            sa.Integer(),
            sa.ForeignKey("ark_compliance_obligations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("tax_year", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("package_hash", sa.String(length=64), nullable=False),
        sa.Column("disclaimer", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("approved_by", sa.String(length=100), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'ready_for_jay', 'approved', 'exported', 'superseded')",
            name="ck_ark_workpaper_status",
        ),
    )
    op.create_index(
        "ix_ark_workpaper_packages_entity_id", "ark_workpaper_packages", ["entity_id"]
    )
    op.create_index("ix_ark_workpaper_packages_tax_year", "ark_workpaper_packages", ["tax_year"])
    op.create_index("ix_ark_workpaper_packages_status", "ark_workpaper_packages", ["status"])


def downgrade() -> None:
    op.drop_table("ark_workpaper_packages")
    op.drop_table("ark_agent_decision_runs")
    op.drop_table("ark_controller_issues")
    op.drop_table("ark_controller_runs")
    op.drop_table("ark_rule_proposals")
    op.drop_table("ark_source_snapshots")
    op.drop_table("ark_authority_sources")
    op.drop_table("ark_evidence_facts")
    op.drop_table("ark_import_runs")
    op.drop_table("ark_entity_relationships")

    with op.batch_alter_table("ark_protected_actions") as batch:
        batch.drop_column("completed_at")
        batch.drop_column("decision_hash")
        batch.drop_column("approval_note")

    with op.batch_alter_table("ark_entity_access") as batch:
        batch.drop_column("protected_approver")

    with op.batch_alter_table("ark_entities") as batch:
        batch.drop_index("ix_ark_entities_facts_status")
        batch.drop_index("ix_ark_entities_posting_mode")
        batch.drop_constraint("ck_ark_entities_facts_status", type_="check")
        batch.drop_constraint("ck_ark_entities_posting_mode", type_="check")
        batch.drop_column("profile")
        batch.drop_column("facts_status")
        batch.drop_column("posting_mode")
