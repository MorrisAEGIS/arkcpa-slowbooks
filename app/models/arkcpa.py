"""Ark CPA control-plane records.

These tables live in the control database named by ``DATABASE_URL``.  Entity
ledgers are separate PostgreSQL databases; only their opaque database name is
stored here.  Raw document bytes stay in Ark Files and never enter these
tables or an agent prompt by default.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ArkEntity(Base):
    __tablename__ = "ark_entities"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('ca_personal', 'ca_ab_corporation', "
            "'ca_ab_family_trust', 'us_tx_llc')",
            name="ck_ark_entities_type",
        ),
        CheckConstraint(
            "status IN ('active', 'dormant', 'archived')",
            name="ck_ark_entities_status",
        ),
        CheckConstraint(
            "posting_mode IN ('draft_only', 'assisted', 'autopost_ordinary', 'frozen')",
            name="ck_ark_entities_posting_mode",
        ),
        CheckConstraint(
            "facts_status IN ('incomplete', 'verified', 'hold')",
            name="ck_ark_entities_facts_status",
        ),
    )

    id = Column(Integer, primary_key=True)
    name = Column(String(240), nullable=False)
    slug = Column(String(80), nullable=False, unique=True, index=True)
    entity_type = Column(String(40), nullable=False, index=True)
    jurisdiction = Column(String(80), nullable=False)
    status = Column(String(20), nullable=False, default="active", index=True)
    database_name = Column(String(63), nullable=False, unique=True)
    ark_files_path = Column(String(500), nullable=False, unique=True)
    currency = Column(String(3), nullable=False, default="CAD")
    fiscal_year_end_month = Column(Integer, nullable=False, default=12)
    fiscal_year_end_day = Column(Integer, nullable=False, default=31)
    payroll_enabled = Column(Boolean, nullable=False, default=False)
    posting_mode = Column(String(30), nullable=False, default="draft_only", index=True)
    facts_status = Column(String(20), nullable=False, default="incomplete", index=True)
    profile = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class ArkEntityAccess(Base):
    __tablename__ = "ark_entity_access"
    __table_args__ = (
        UniqueConstraint("entity_id", "user_id", name="uq_ark_entity_user"),
        CheckConstraint(
            "role IN ('admin', 'bookkeeper', 'readonly')",
            name="ck_ark_entity_access_role",
        ),
    )

    id = Column(Integer, primary_key=True)
    entity_id = Column(
        Integer, ForeignKey("ark_entities.id", ondelete="CASCADE"), nullable=False
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role = Column(String(20), nullable=False)
    is_default = Column(Boolean, nullable=False, default=False)
    protected_approver = Column(Boolean, nullable=False, default=False)
    granted_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class ArkAgentGrant(Base):
    __tablename__ = "ark_agent_grants"
    __table_args__ = (
        UniqueConstraint("entity_id", "token_id", name="uq_ark_entity_token"),
        CheckConstraint(
            "agent_role IN ('bookkeeper', 'controller')",
            name="ck_ark_agent_grant_role",
        ),
    )

    id = Column(Integer, primary_key=True)
    entity_id = Column(
        Integer, ForeignKey("ark_entities.id", ondelete="CASCADE"), nullable=False
    )
    token_id = Column(
        Integer, ForeignKey("api_tokens.id", ondelete="CASCADE"), nullable=False
    )
    agent_role = Column(String(20), nullable=False)
    model_id = Column(String(160), nullable=False)
    model_family = Column(String(100), nullable=False)
    prompt_version = Column(String(80), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    granted_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class ArkEvidence(Base):
    __tablename__ = "ark_evidence"
    __table_args__ = (
        UniqueConstraint("entity_id", "source_path", name="uq_ark_evidence_path"),
        CheckConstraint(
            "status IN ('indexed', 'quarantined', 'extracted', 'verified', 'rejected')",
            name="ck_ark_evidence_status",
        ),
    )

    id = Column(Integer, primary_key=True)
    entity_id = Column(
        Integer,
        ForeignKey("ark_entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_path = Column(String(1000), nullable=False)
    category = Column(String(30), nullable=False, default="Inbox", index=True)
    content_hash = Column(String(64), nullable=True, index=True)
    size_bytes = Column(Integer, nullable=False, default=0)
    mime_type = Column(String(120), nullable=False, default="application/octet-stream")
    status = Column(String(20), nullable=False, default="indexed", index=True)
    quarantine_reason = Column(String(240), nullable=True)
    extracted_text_hash = Column(String(64), nullable=True)
    extractor_version = Column(String(80), nullable=True)
    modified_at = Column(DateTime(timezone=True), nullable=True)
    discovered_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    reviewed_by = Column(String(100), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)


class ArkPostingCandidate(Base):
    __tablename__ = "ark_posting_candidates"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'reviewing', 'ready', 'blocked', 'posting', "
            "'posted', 'rejected')",
            name="ck_ark_posting_candidate_status",
        ),
    )

    id = Column(Integer, primary_key=True)
    entity_id = Column(
        Integer,
        ForeignKey("ark_entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    evidence_id = Column(
        Integer,
        ForeignKey("ark_evidence.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    transaction_date = Column(Date, nullable=False)
    description = Column(Text, nullable=False)
    reference = Column(String(100), nullable=True)
    currency = Column(String(3), nullable=False, default="CAD")
    lines = Column(JSON, nullable=False)
    tax_context = Column(JSON, nullable=False, default=dict)
    action_type = Column(String(40), nullable=False, default="ordinary_entry")
    status = Column(String(20), nullable=False, default="draft", index=True)
    deterministic_checks = Column(JSON, nullable=False, default=dict)
    block_reason = Column(String(500), nullable=True)
    posted_transaction_id = Column(Integer, nullable=True)
    created_by = Column(String(100), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class ArkAgentDecision(Base):
    __tablename__ = "ark_agent_decisions"
    __table_args__ = (
        UniqueConstraint("candidate_id", "agent_role", name="uq_ark_candidate_role"),
        CheckConstraint(
            "agent_role IN ('bookkeeper', 'controller')",
            name="ck_ark_agent_decision_role",
        ),
        CheckConstraint(
            "decision IN ('approve', 'reject', 'escalate')",
            name="ck_ark_agent_decision_value",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_ark_agent_decision_confidence",
        ),
    )

    id = Column(Integer, primary_key=True)
    candidate_id = Column(
        Integer,
        ForeignKey("ark_posting_candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_role = Column(String(20), nullable=False)
    model_id = Column(String(160), nullable=False)
    model_family = Column(String(100), nullable=False)
    prompt_version = Column(String(80), nullable=False)
    policy_version = Column(String(80), nullable=False)
    confidence = Column(Numeric(5, 4), nullable=False)
    decision = Column(String(20), nullable=False)
    evidence_hash = Column(String(64), nullable=False)
    rationale_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class ArkProtectedAction(Base):
    __tablename__ = "ark_protected_actions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('required', 'approved', 'rejected', 'completed')",
            name="ck_ark_protected_action_status",
        ),
    )

    id = Column(Integer, primary_key=True)
    entity_id = Column(
        Integer,
        ForeignKey("ark_entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    candidate_id = Column(
        Integer,
        ForeignKey("ark_posting_candidates.id", ondelete="SET NULL"),
        nullable=True,
    )
    action_type = Column(String(60), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="required", index=True)
    reason = Column(String(500), nullable=False)
    requested_by = Column(String(100), nullable=False)
    approved_by = Column(String(100), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    approval_note = Column(String(1000), nullable=True)
    decision_hash = Column(String(64), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class ArkComplianceObligation(Base):
    __tablename__ = "ark_compliance_obligations"
    __table_args__ = (
        UniqueConstraint(
            "entity_id", "code", "tax_year", name="uq_ark_obligation_period"
        ),
        CheckConstraint(
            "status IN ('monitoring', 'open', 'prepared', 'filed', 'not_applicable')",
            name="ck_ark_obligation_status",
        ),
    )

    id = Column(Integer, primary_key=True)
    entity_id = Column(
        Integer,
        ForeignKey("ark_entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code = Column(String(80), nullable=False)
    label = Column(String(240), nullable=False)
    authority = Column(String(120), nullable=False)
    tax_year = Column(Integer, nullable=False)
    due_date = Column(Date, nullable=True, index=True)
    status = Column(String(20), nullable=False, default="monitoring", index=True)
    human_required = Column(Boolean, nullable=False, default=True)
    notes = Column(Text, nullable=True)
    updated_at = Column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class ArkEntityRelationship(Base):
    __tablename__ = "ark_entity_relationships"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'historical', 'unverified')",
            name="ck_ark_entity_relationship_status",
        ),
    )

    id = Column(Integer, primary_key=True)
    entity_id = Column(
        Integer, ForeignKey("ark_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    related_entity_id = Column(
        Integer, ForeignKey("ark_entities.id", ondelete="SET NULL"), nullable=True
    )
    subject_label = Column(String(240), nullable=False)
    relationship_type = Column(String(80), nullable=False, index=True)
    effective_from = Column(Date, nullable=True)
    effective_to = Column(Date, nullable=True)
    status = Column(String(20), nullable=False, default="unverified")
    source_evidence_id = Column(
        Integer, ForeignKey("ark_evidence.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class ArkImportRun(Base):
    __tablename__ = "ark_import_runs"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('ark_files', 'bank_file', 'mail', 'tax_source')",
            name="ck_ark_import_run_kind",
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'blocked', 'failed')",
            name="ck_ark_import_run_status",
        ),
    )

    id = Column(Integer, primary_key=True)
    entity_id = Column(
        Integer, ForeignKey("ark_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind = Column(String(20), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="running", index=True)
    counts = Column(JSON, nullable=False, default=dict)
    manifest_hash = Column(String(64), nullable=True)
    error_code = Column(String(80), nullable=True)
    error_message = Column(String(500), nullable=True)
    started_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class ArkEvidenceFact(Base):
    __tablename__ = "ark_evidence_facts"
    __table_args__ = (
        UniqueConstraint("evidence_id", "fact_key", "locator", name="uq_ark_evidence_fact"),
        CheckConstraint(
            "status IN ('extracted', 'verified', 'rejected')",
            name="ck_ark_evidence_fact_status",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_ark_evidence_fact_confidence",
        ),
    )

    id = Column(Integer, primary_key=True)
    evidence_id = Column(
        Integer, ForeignKey("ark_evidence.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fact_key = Column(String(100), nullable=False, index=True)
    value = Column(JSON, nullable=False)
    confidence = Column(Numeric(5, 4), nullable=False)
    locator = Column(String(240), nullable=False, default="document")
    source_hash = Column(String(64), nullable=False)
    extractor_version = Column(String(80), nullable=False)
    status = Column(String(20), nullable=False, default="extracted", index=True)
    reviewed_by = Column(String(100), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class ArkAuthoritySource(Base):
    __tablename__ = "ark_authority_sources"
    __table_args__ = (
        UniqueConstraint("code", name="uq_ark_authority_source_code"),
        CheckConstraint(
            "authority_level IN ('law', 'regulation', 'court', 'administrative', "
            "'proposed', 'lead')",
            name="ck_ark_authority_source_level",
        ),
    )

    id = Column(Integer, primary_key=True)
    code = Column(String(100), nullable=False)
    title = Column(String(300), nullable=False)
    jurisdiction = Column(String(80), nullable=False, index=True)
    authority_level = Column(String(30), nullable=False, index=True)
    url = Column(String(1000), nullable=False)
    license_note = Column(String(300), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class ArkSourceSnapshot(Base):
    __tablename__ = "ark_source_snapshots"
    __table_args__ = (
        UniqueConstraint("source_id", "content_hash", name="uq_ark_source_snapshot_hash"),
        CheckConstraint(
            "status IN ('current', 'superseded', 'proposed', 'failed')",
            name="ck_ark_source_snapshot_status",
        ),
    )

    id = Column(Integer, primary_key=True)
    source_id = Column(
        Integer, ForeignKey("ark_authority_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content_hash = Column(String(64), nullable=False, index=True)
    retrieved_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    effective_from = Column(Date, nullable=True)
    effective_to = Column(Date, nullable=True)
    status = Column(String(20), nullable=False, default="current", index=True)
    snapshot_metadata = Column(JSON, nullable=False, default=dict)


class ArkRuleProposal(Base):
    __tablename__ = "ark_rule_proposals"
    __table_args__ = (
        UniqueConstraint("source_snapshot_id", "code", name="uq_ark_rule_proposal"),
        CheckConstraint(
            "status IN ('draft', 'testing', 'approved', 'rejected', 'promoted')",
            name="ck_ark_rule_proposal_status",
        ),
    )

    id = Column(Integer, primary_key=True)
    source_snapshot_id = Column(
        Integer, ForeignKey("ark_source_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code = Column(String(120), nullable=False)
    title = Column(String(300), nullable=False)
    proposed_change = Column(JSON, nullable=False)
    status = Column(String(20), nullable=False, default="draft", index=True)
    test_results = Column(JSON, nullable=False, default=dict)
    requested_by = Column(String(100), nullable=False, default="tax-research-agent")
    approved_by = Column(String(100), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class ArkControllerRun(Base):
    __tablename__ = "ark_controller_runs"
    __table_args__ = (
        CheckConstraint(
            "run_type IN ('daily', 'weekly', 'monthly', 'annual', 'candidate')",
            name="ck_ark_controller_run_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'blocked', 'failed')",
            name="ck_ark_controller_run_status",
        ),
    )

    id = Column(Integer, primary_key=True)
    entity_id = Column(
        Integer, ForeignKey("ark_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_type = Column(String(20), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="queued", index=True)
    policy_version = Column(String(80), nullable=False)
    summary = Column(JSON, nullable=False, default=dict)
    error_code = Column(String(80), nullable=True)
    started_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class ArkControllerIssue(Base):
    __tablename__ = "ark_controller_issues"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('info', 'low', 'medium', 'high', 'critical')",
            name="ck_ark_controller_issue_severity",
        ),
        CheckConstraint(
            "status IN ('open', 'acknowledged', 'resolved', 'dismissed')",
            name="ck_ark_controller_issue_status",
        ),
    )

    id = Column(Integer, primary_key=True)
    run_id = Column(
        Integer, ForeignKey("ark_controller_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    entity_id = Column(
        Integer, ForeignKey("ark_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_id = Column(
        Integer, ForeignKey("ark_evidence.id", ondelete="SET NULL"), nullable=True
    )
    category = Column(String(80), nullable=False, index=True)
    severity = Column(String(20), nullable=False, default="medium", index=True)
    status = Column(String(20), nullable=False, default="open", index=True)
    title = Column(String(300), nullable=False)
    detail = Column(Text, nullable=False)
    due_date = Column(Date, nullable=True)
    assigned_to = Column(String(100), nullable=True)
    resolved_by = Column(String(100), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class ArkAgentDecisionRun(Base):
    __tablename__ = "ark_agent_decision_runs"
    __table_args__ = (
        CheckConstraint(
            "agent_role IN ('bookkeeper', 'controller')",
            name="ck_ark_agent_decision_run_role",
        ),
        CheckConstraint(
            "decision IN ('approve', 'reject', 'escalate')",
            name="ck_ark_agent_decision_run_value",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_ark_agent_decision_run_confidence",
        ),
    )

    id = Column(Integer, primary_key=True)
    candidate_id = Column(
        Integer, ForeignKey("ark_posting_candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    controller_run_id = Column(
        Integer, ForeignKey("ark_controller_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_role = Column(String(20), nullable=False)
    model_id = Column(String(160), nullable=False)
    model_family = Column(String(100), nullable=False)
    prompt_version = Column(String(80), nullable=False)
    policy_version = Column(String(80), nullable=False)
    confidence = Column(Numeric(5, 4), nullable=False)
    decision = Column(String(20), nullable=False)
    evidence_hash = Column(String(64), nullable=False)
    context_hash = Column(String(64), nullable=False)
    rationale_hash = Column(String(64), nullable=False)
    response_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)


class ArkWorkpaperPackage(Base):
    __tablename__ = "ark_workpaper_packages"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'ready_for_jay', 'approved', 'exported', 'superseded')",
            name="ck_ark_workpaper_status",
        ),
    )

    id = Column(Integer, primary_key=True)
    entity_id = Column(
        Integer, ForeignKey("ark_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    obligation_id = Column(
        Integer, ForeignKey("ark_compliance_obligations.id", ondelete="SET NULL"), nullable=True
    )
    tax_year = Column(Integer, nullable=False, index=True)
    status = Column(String(30), nullable=False, default="draft", index=True)
    manifest = Column(JSON, nullable=False, default=dict)
    package_hash = Column(String(64), nullable=False)
    disclaimer = Column(
        Text,
        nullable=False,
        default="Prepared for owner and licensed-professional review; not filed or submitted.",
    )
    created_by = Column(String(100), nullable=False)
    approved_by = Column(String(100), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
