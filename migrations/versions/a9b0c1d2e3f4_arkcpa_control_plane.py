"""add Ark CPA entity, evidence, agent, and compliance control plane

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-09-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a9b0c1d2e3f4"
down_revision: Union[str, None] = "f8a9b0c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # User-management and OIDC paths normalize email to lowercase. Enforce
    # the one-email/one-principal invariant in the database too, closing the
    # small race window between the application uniqueness check and commit.
    with op.batch_alter_table("users") as batch:
        batch.create_unique_constraint("uq_users_email", ["email"])

    op.create_table(
        "ark_entities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("jurisdiction", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("database_name", sa.String(length=63), nullable=False),
        sa.Column("ark_files_path", sa.String(length=500), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("fiscal_year_end_month", sa.Integer(), nullable=False),
        sa.Column("fiscal_year_end_day", sa.Integer(), nullable=False),
        sa.Column("payroll_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "entity_type IN ('ca_personal', 'ca_ab_corporation', "
            "'ca_ab_family_trust', 'us_tx_llc')",
            name="ck_ark_entities_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'dormant', 'archived')",
            name="ck_ark_entities_status",
        ),
        sa.UniqueConstraint("slug"),
        sa.UniqueConstraint("database_name"),
        sa.UniqueConstraint("ark_files_path"),
    )
    op.create_index("ix_ark_entities_slug", "ark_entities", ["slug"])
    op.create_index("ix_ark_entities_type", "ark_entities", ["entity_type"])
    op.create_index("ix_ark_entities_status", "ark_entities", ["status"])

    op.create_table(
        "ark_entity_access",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "entity_id",
            sa.Integer(),
            sa.ForeignKey("ark_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('admin', 'bookkeeper', 'readonly')",
            name="ck_ark_entity_access_role",
        ),
        sa.UniqueConstraint("entity_id", "user_id", name="uq_ark_entity_user"),
    )

    op.create_table(
        "ark_evidence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "entity_id",
            sa.Integer(),
            sa.ForeignKey("ark_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_path", sa.String(length=1000), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("quarantine_reason", sa.String(length=240), nullable=True),
        sa.Column("extracted_text_hash", sa.String(length=64), nullable=True),
        sa.Column("extractor_version", sa.String(length=80), nullable=True),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(length=100), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('indexed', 'quarantined', 'extracted', 'verified', 'rejected')",
            name="ck_ark_evidence_status",
        ),
        sa.UniqueConstraint("entity_id", "source_path", name="uq_ark_evidence_path"),
    )
    op.create_index("ix_ark_evidence_entity_id", "ark_evidence", ["entity_id"])
    op.create_index("ix_ark_evidence_category", "ark_evidence", ["category"])
    op.create_index("ix_ark_evidence_content_hash", "ark_evidence", ["content_hash"])
    op.create_index("ix_ark_evidence_status", "ark_evidence", ["status"])

    op.create_table(
        "ark_agent_grants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "entity_id",
            sa.Integer(),
            sa.ForeignKey("ark_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "token_id",
            sa.Integer(),
            sa.ForeignKey("api_tokens.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_role", sa.String(length=20), nullable=False),
        sa.Column("model_id", sa.String(length=160), nullable=False),
        sa.Column("model_family", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "agent_role IN ('bookkeeper', 'controller')",
            name="ck_ark_agent_grant_role",
        ),
        sa.UniqueConstraint("entity_id", "token_id", name="uq_ark_entity_token"),
    )

    op.create_table(
        "ark_posting_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "entity_id",
            sa.Integer(),
            sa.ForeignKey("ark_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "evidence_id",
            sa.Integer(),
            sa.ForeignKey("ark_evidence.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("reference", sa.String(length=100), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("lines", sa.JSON(), nullable=False),
        sa.Column("tax_context", sa.JSON(), nullable=False),
        sa.Column("action_type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("deterministic_checks", sa.JSON(), nullable=False),
        sa.Column("block_reason", sa.String(length=500), nullable=True),
        sa.Column("posted_transaction_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'reviewing', 'ready', 'blocked', 'posting', "
            "'posted', 'rejected')",
            name="ck_ark_posting_candidate_status",
        ),
    )
    op.create_index(
        "ix_ark_posting_candidates_entity_id", "ark_posting_candidates", ["entity_id"]
    )
    op.create_index(
        "ix_ark_posting_candidates_evidence_id",
        "ark_posting_candidates",
        ["evidence_id"],
    )
    op.create_index(
        "ix_ark_posting_candidates_status", "ark_posting_candidates", ["status"]
    )

    op.create_table(
        "ark_agent_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.Integer(),
            sa.ForeignKey("ark_posting_candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_role", sa.String(length=20), nullable=False),
        sa.Column("model_id", sa.String(length=160), nullable=False),
        sa.Column("model_family", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("rationale_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "agent_role IN ('bookkeeper', 'controller')",
            name="ck_ark_agent_decision_role",
        ),
        sa.CheckConstraint(
            "decision IN ('approve', 'reject', 'escalate')",
            name="ck_ark_agent_decision_value",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_ark_agent_decision_confidence",
        ),
        sa.UniqueConstraint("candidate_id", "agent_role", name="uq_ark_candidate_role"),
    )
    op.create_index(
        "ix_ark_agent_decisions_candidate_id", "ark_agent_decisions", ["candidate_id"]
    )

    op.create_table(
        "ark_protected_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "entity_id",
            sa.Integer(),
            sa.ForeignKey("ark_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.Integer(),
            sa.ForeignKey("ark_posting_candidates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action_type", sa.String(length=60), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("requested_by", sa.String(length=100), nullable=False),
        sa.Column("approved_by", sa.String(length=100), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('required', 'approved', 'rejected', 'completed')",
            name="ck_ark_protected_action_status",
        ),
    )
    op.create_index(
        "ix_ark_protected_actions_entity_id", "ark_protected_actions", ["entity_id"]
    )
    op.create_index(
        "ix_ark_protected_actions_action_type", "ark_protected_actions", ["action_type"]
    )
    op.create_index(
        "ix_ark_protected_actions_status", "ark_protected_actions", ["status"]
    )

    op.create_table(
        "ark_compliance_obligations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "entity_id",
            sa.Integer(),
            sa.ForeignKey("ark_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("label", sa.String(length=240), nullable=False),
        sa.Column("authority", sa.String(length=120), nullable=False),
        sa.Column("tax_year", sa.Integer(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("human_required", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "entity_id", "code", "tax_year", name="uq_ark_obligation_period"
        ),
        sa.CheckConstraint(
            "status IN ('monitoring', 'open', 'prepared', 'filed', 'not_applicable')",
            name="ck_ark_obligation_status",
        ),
    )
    op.create_index(
        "ix_ark_compliance_obligations_entity_id",
        "ark_compliance_obligations",
        ["entity_id"],
    )
    op.create_index(
        "ix_ark_compliance_obligations_due_date",
        "ark_compliance_obligations",
        ["due_date"],
    )
    op.create_index(
        "ix_ark_compliance_obligations_status",
        "ark_compliance_obligations",
        ["status"],
    )


def downgrade() -> None:
    op.drop_table("ark_compliance_obligations")
    op.drop_table("ark_protected_actions")
    op.drop_table("ark_agent_decisions")
    op.drop_table("ark_posting_candidates")
    op.drop_table("ark_evidence")
    op.drop_table("ark_agent_grants")
    op.drop_table("ark_entity_access")
    op.drop_table("ark_entities")
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("uq_users_email", type_="unique")
