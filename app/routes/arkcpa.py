"""Unified Ark CPA entity, evidence, agent-review, and compliance API."""

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import (
    entity_mode_enabled,
    entity_session_factory,
    get_control_db,
    get_db,
)
from app.models.api_tokens import ApiToken
from app.models.arkcpa import (
    ArkAgentDecision,
    ArkAgentDecisionRun,
    ArkAgentGrant,
    ArkAuthoritySource,
    ArkComplianceObligation,
    ArkControllerIssue,
    ArkControllerRun,
    ArkEntity,
    ArkEntityAccess,
    ArkEvidence,
    ArkEvidenceFact,
    ArkPostingCandidate,
    ArkProtectedAction,
    ArkRuleProposal,
    ArkSourceSnapshot,
    ArkWorkpaperPackage,
)
from app.models.transactions import Transaction
from app.models.users import User
from app.schemas.common import StrictModel
from app.services.accounting import create_journal_entry
from app.services.arkcpa_controller import (
    ARKCPA_LEDGER_SOURCE_TYPE,
    run_candidate_review,
)
from app.services.arkcpa_ingestion import scan_and_extract_entity
from app.services.arkcpa_policy import evaluate_candidate
from app.services.arkcpa_rules import (
    ENTITY_TYPES,
    MIN_AUTONOMOUS_CONFIDENCE,
    OBLIGATION_PACKS,
    POLICY_VERSION,
    PROTECTED_ACTIONS,
    is_protected_action,
    obligations_for,
    validate_entity_profile,
)
from app.services.arkcpa_tax import (
    refresh_all_authority_sources,
    seed_authority_sources,
)
from app.services.company_service import create_company
from app.services.settings_service import set_setting

router = APIRouter(prefix="/api/ark-cpa", tags=["ark-cpa"])
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,78}[a-z0-9]$")
_DATABASE_RE = re.compile(r"^[a-z][a-z0-9_]{1,61}[a-z0-9]$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _principal(request: Request) -> tuple[str, str, str | None]:
    """Return (kind, name, role); middleware has authenticated it already."""
    token = getattr(request.state, "token_principal", None)
    if isinstance(token, dict) and token.get("username"):
        return "token", token["username"].removeprefix("token:"), token.get("role")
    return (
        "user",
        request.session.get("username") or "operator",
        request.session.get("role") or "admin",
    )


def _require_human(request: Request) -> str:
    kind, name, _role = _principal(request)
    if kind != "user":
        raise HTTPException(status_code=403, detail="Human account required")
    return name


def _require_admin(request: Request) -> str:
    name = _require_human(request)
    if _principal(request)[2] != "admin":
        raise HTTPException(status_code=403, detail="Human admin required")
    return name


def _require_entity_admin(db: Session, request: Request, entity_id: int) -> str:
    name = _require_human(request)
    access = _human_access(db, name, entity_id)
    if access is None or access.role != "admin":
        raise HTTPException(status_code=403, detail="Entity admin required")
    return name


def _require_protected_approver(db: Session, request: Request, entity_id: int) -> str:
    name = _require_entity_admin(db, request, entity_id)
    access = _human_access(db, name, entity_id)
    if access is None or not access.protected_approver:
        raise HTTPException(status_code=403, detail="Protected owner approval required")
    return name


def _require_any_protected_approver(db: Session, request: Request) -> str:
    name = _require_human(request)
    row = (
        db.query(ArkEntityAccess)
        .join(User, User.id == ArkEntityAccess.user_id)
        .filter(
            User.username == name,
            User.is_active,
            ArkEntityAccess.role == "admin",
            ArkEntityAccess.protected_approver,
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=403, detail="Protected owner approval required")
    return name


def _human_access(db: Session, username: str, entity_id: int) -> ArkEntityAccess | None:
    return (
        db.query(ArkEntityAccess)
        .join(User, User.id == ArkEntityAccess.user_id)
        .filter(
            User.username == username,
            User.is_active,
            ArkEntityAccess.entity_id == entity_id,
        )
        .first()
    )


def _agent_access(db: Session, label: str, entity_id: int) -> ArkAgentGrant | None:
    return (
        db.query(ArkAgentGrant)
        .join(ApiToken, ApiToken.id == ArkAgentGrant.token_id)
        .filter(
            ApiToken.label == label,
            ApiToken.is_active,
            ArkAgentGrant.entity_id == entity_id,
            ArkAgentGrant.is_active,
        )
        .first()
    )


def _require_entity_access(
    db: Session,
    request: Request,
    entity_id: int,
    *,
    write: bool = False,
    agent_role: str | None = None,
) -> str:
    kind, name, role = _principal(request)
    if kind == "token":
        grant = _agent_access(db, name, entity_id)
        if grant is None or (agent_role and grant.agent_role != agent_role):
            raise HTTPException(
                status_code=403, detail="Token is not granted to this entity and role"
            )
        return name
    access = _human_access(db, name, entity_id)
    if access is None:
        raise HTTPException(status_code=403, detail="No access to this entity")
    if write and access.role == "readonly":
        raise HTTPException(status_code=403, detail="Read-only entity access")
    return name


def _entity_out(
    entity: ArkEntity,
    role: str | None = None,
    active: bool = False,
    protected_approver: bool = False,
) -> dict:
    return {
        "id": entity.id,
        "name": entity.name,
        "slug": entity.slug,
        "entity_type": entity.entity_type,
        "entity_type_label": ENTITY_TYPES.get(entity.entity_type, {}).get(
            "label", entity.entity_type
        ),
        "jurisdiction": entity.jurisdiction,
        "status": entity.status,
        "posting_mode": entity.posting_mode,
        "facts_status": entity.facts_status,
        "profile": entity.profile or {},
        "currency": entity.currency,
        "fiscal_year_end": f"{entity.fiscal_year_end_month:02d}-{entity.fiscal_year_end_day:02d}",
        "ark_files_path": entity.ark_files_path,
        "payroll_mode": (
            "evidence-only" if not entity.payroll_enabled else "human-controlled"
        ),
        "access_role": role,
        "protected_approver": protected_approver,
        "active": active,
    }


def _candidate_out(db: Session, candidate: ArkPostingCandidate) -> dict:
    decisions = (
        db.query(ArkAgentDecision)
        .filter(ArkAgentDecision.candidate_id == candidate.id)
        .order_by(ArkAgentDecision.agent_role)
        .all()
    )
    return {
        "id": candidate.id,
        "entity_id": candidate.entity_id,
        "evidence_id": candidate.evidence_id,
        "transaction_date": candidate.transaction_date.isoformat(),
        "description": candidate.description,
        "reference": candidate.reference or "",
        "currency": candidate.currency,
        "lines": candidate.lines,
        "tax_context": candidate.tax_context,
        "action_type": candidate.action_type,
        "status": candidate.status,
        "deterministic_checks": candidate.deterministic_checks,
        "block_reason": candidate.block_reason,
        "posted_transaction_id": candidate.posted_transaction_id,
        "decisions": [
            {
                "agent_role": row.agent_role,
                "model_id": row.model_id,
                "model_family": row.model_family,
                "confidence": float(row.confidence),
                "decision": row.decision,
                "prompt_version": row.prompt_version,
                "policy_version": row.policy_version,
                "evidence_hash": row.evidence_hash,
            }
            for row in decisions
        ],
    }


class EntityCreate(StrictModel):
    name: str = Field(..., min_length=2, max_length=240)
    slug: str = Field(..., min_length=3, max_length=80)
    entity_type: Literal[
        "ca_personal", "ca_ab_corporation", "ca_ab_family_trust", "us_tx_llc"
    ]
    database_name: str = Field(..., min_length=3, max_length=63)
    jurisdiction: str = Field(..., min_length=2, max_length=80)
    currency: Literal["CAD", "USD"]
    fiscal_year_end_month: int = Field(12, ge=1, le=12)
    fiscal_year_end_day: int = Field(31, ge=1, le=31)
    status: Literal["active", "dormant"] = "active"

    @model_validator(mode="after")
    def valid_fiscal_year_end(self):
        try:
            datetime(2000, self.fiscal_year_end_month, self.fiscal_year_end_day)
        except ValueError as exc:
            raise ValueError("Fiscal year end must be a valid month and day") from exc
        return self


class AccessGrant(StrictModel):
    username: str = Field(..., min_length=1, max_length=100)
    role: Literal["admin", "bookkeeper", "readonly"]
    is_default: bool = False
    protected_approver: bool = False


class EntityGovernanceUpdate(StrictModel):
    posting_mode: Literal["draft_only", "assisted", "autopost_ordinary", "frozen"]
    facts_status: Literal["incomplete", "verified", "hold"]
    profile: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def profile_excludes_sensitive_values(self):
        validate_entity_profile(self.profile)
        return self


class AgentGrantIn(StrictModel):
    token_id: int
    agent_role: Literal["bookkeeper", "controller"]
    model_id: str = Field(..., min_length=1, max_length=160)
    model_family: str = Field(..., min_length=1, max_length=100)
    prompt_version: str = Field(..., min_length=1, max_length=80)


class EvidenceReview(StrictModel):
    status: Literal["verified", "rejected"]
    extracted_text_hash: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")
    extractor_version: str | None = Field(None, max_length=80)


class CandidateLine(StrictModel):
    account_id: int
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    description: str = Field("", max_length=300)


class CandidateCreate(StrictModel):
    evidence_id: int
    transaction_date: str
    description: str = Field(..., min_length=1, max_length=1000)
    reference: str | None = Field(None, max_length=100)
    currency: Literal["CAD", "USD"]
    lines: list[CandidateLine] = Field(..., min_length=2, max_length=200)
    tax_context: dict = Field(default_factory=dict)
    action_type: str = Field("ordinary_entry", max_length=40)


class AgentDecisionIn(StrictModel):
    agent_role: Literal["bookkeeper", "controller"]
    model_id: str = Field(..., min_length=1, max_length=160)
    model_family: str = Field(..., min_length=1, max_length=100)
    prompt_version: str = Field(..., min_length=1, max_length=80)
    confidence: Decimal = Field(..., ge=0, le=1)
    decision: Literal["approve", "reject", "escalate"]
    rationale: str = Field(..., min_length=1, max_length=20000)


class ProtectedDecisionIn(StrictModel):
    decision: Literal["approved", "rejected"]
    note: str = Field(..., min_length=3, max_length=1000)


class WorkpaperCreate(StrictModel):
    obligation_id: int | None = None
    tax_year: int = Field(..., ge=2000, le=2200)
    evidence_ids: list[int] = Field(..., min_length=1, max_length=1000)


@router.get("/status")
def arkcpa_status(request: Request, db: Session = Depends(get_control_db)):
    entities = list_entities(request, db)
    entity_ids = [row["id"] for row in entities]
    counts = {
        "evidence": 0,
        "review_queue": 0,
        "protected_actions": 0,
        "obligations": 0,
        "controller_issues": 0,
        "tax_changes": 0,
        "workpapers": 0,
    }
    if entity_ids:
        counts = {
            "evidence": db.query(ArkEvidence)
            .filter(ArkEvidence.entity_id.in_(entity_ids))
            .count(),
            "review_queue": db.query(ArkPostingCandidate)
            .filter(
                ArkPostingCandidate.entity_id.in_(entity_ids),
                ArkPostingCandidate.status.in_(
                    ("draft", "reviewing", "ready", "blocked")
                ),
            )
            .count(),
            "protected_actions": db.query(ArkProtectedAction)
            .filter(
                ArkProtectedAction.entity_id.in_(entity_ids),
                ArkProtectedAction.status == "required",
            )
            .count(),
            "obligations": db.query(ArkComplianceObligation)
            .filter(
                ArkComplianceObligation.entity_id.in_(entity_ids),
                ArkComplianceObligation.status.in_(("monitoring", "open", "prepared")),
            )
            .count(),
            "controller_issues": db.query(ArkControllerIssue)
            .filter(
                ArkControllerIssue.entity_id.in_(entity_ids),
                ArkControllerIssue.status.in_(("open", "acknowledged")),
            )
            .count(),
            "tax_changes": db.query(ArkRuleProposal)
            .filter(ArkRuleProposal.status.in_(("draft", "testing", "approved")))
            .count(),
            "workpapers": db.query(ArkWorkpaperPackage)
            .filter(
                ArkWorkpaperPackage.entity_id.in_(entity_ids),
                ArkWorkpaperPackage.status.in_(("draft", "ready_for_jay")),
            )
            .count(),
        }
    return {
        "product": "Ark CPA",
        "entity_mode": entity_mode_enabled(),
        "policy_version": POLICY_VERSION,
        "autonomous_confidence_threshold": MIN_AUTONOMOUS_CONFIDENCE,
        "raw_documents_in_model_memory": False,
        "cloud_fallback": "redacted-only",
        "model_gateway": "internal-only",
        "automatic_tax_rule_promotion": False,
        "filing_and_money_movement": "human-protected",
        "payroll": "evidence-and-reminders-only",
        "counts": counts,
        "entities": entities,
    }


@router.get("/rule-packs")
def rule_packs():
    return {
        "policy_version": POLICY_VERSION,
        "entity_types": ENTITY_TYPES,
        "protected_actions": PROTECTED_ACTIONS,
        "obligation_codes": {
            key: [row[0] for row in rows] for key, rows in OBLIGATION_PACKS.items()
        },
    }


@router.get("/entities")
def list_entities(request: Request, db: Session = Depends(get_control_db)):
    kind, name, _role = _principal(request)
    active_slug = request.headers.get("x-ark-entity") or request.session.get(
        "active_entity_slug"
    )
    if kind == "token":
        rows = (
            db.query(ArkEntity, ArkAgentGrant)
            .join(ArkAgentGrant, ArkAgentGrant.entity_id == ArkEntity.id)
            .join(ApiToken, ApiToken.id == ArkAgentGrant.token_id)
            .filter(ApiToken.label == name, ApiToken.is_active, ArkAgentGrant.is_active)
            .order_by(ArkEntity.name)
            .all()
        )
    else:
        rows = (
            db.query(ArkEntity, ArkEntityAccess)
            .join(ArkEntityAccess, ArkEntityAccess.entity_id == ArkEntity.id)
            .join(User, User.id == ArkEntityAccess.user_id)
            .filter(User.username == name, User.is_active)
            .order_by(ArkEntity.name)
            .all()
        )
    return [
        _entity_out(
            entity,
            access.agent_role if kind == "token" else access.role,
            entity.slug == active_slug,
            False if kind == "token" else bool(access.protected_approver),
        )
        for entity, access in rows
    ]


@router.post("/entities", status_code=201)
def create_entity(
    data: EntityCreate, request: Request, db: Session = Depends(get_control_db)
):
    creator = _require_admin(request)
    slug = data.slug.strip().lower()
    database_name = data.database_name.strip().lower()
    if not _SLUG_RE.fullmatch(slug):
        raise HTTPException(
            status_code=400,
            detail="Slug must use lowercase letters, numbers, and hyphens",
        )
    if not _DATABASE_RE.fullmatch(database_name):
        raise HTTPException(
            status_code=400,
            detail="Database name must use lowercase letters, numbers, and underscores",
        )
    expected = ENTITY_TYPES[data.entity_type]
    if data.currency != expected["currency"]:
        raise HTTPException(
            status_code=400,
            detail=f"{data.entity_type} ledgers use {expected['currency']}",
        )
    if (
        db.query(ArkEntity)
        .filter((ArkEntity.slug == slug) | (ArkEntity.database_name == database_name))
        .first()
    ):
        raise HTTPException(
            status_code=409, detail="Entity slug or database already exists"
        )

    user = db.query(User).filter(User.username == creator, User.is_active).first()
    if user is None:
        raise HTTPException(
            status_code=409, detail="Admin principal is not materialized"
        )
    if db.query(ArkEntity.id).first() is not None:
        _require_any_protected_approver(db, request)

    if entity_mode_enabled():
        result = create_company(
            db, data.name.strip(), database_name, f"Ark CPA {data.entity_type}"
        )
        if not result.get("success"):
            raise HTTPException(
                status_code=409,
                detail=result.get("error", "Ledger provisioning failed"),
            )
        with entity_session_factory(database_name)() as ledger:
            set_setting(ledger, "company_name", data.name.strip())
            set_setting(ledger, "home_currency", data.currency)
            ledger.commit()

    entity = ArkEntity(
        name=data.name.strip(),
        slug=slug,
        entity_type=data.entity_type,
        jurisdiction=data.jurisdiction.strip(),
        status=data.status,
        database_name=database_name,
        ark_files_path=f"_shared/Ark CPA/{slug}",
        currency=data.currency,
        fiscal_year_end_month=data.fiscal_year_end_month,
        fiscal_year_end_day=data.fiscal_year_end_day,
        payroll_enabled=False,
        posting_mode="draft_only" if data.status == "dormant" else "assisted",
        facts_status="incomplete",
        profile={},
    )
    db.add(entity)
    db.flush()
    first_membership = (
        db.query(ArkEntityAccess).filter(ArkEntityAccess.user_id == user.id).count()
        == 0
    )
    db.add(
        ArkEntityAccess(
            entity_id=entity.id,
            user_id=user.id,
            role="admin",
            is_default=first_membership,
            protected_approver=True,
        )
    )
    for obligation in obligations_for(entity.entity_type):
        db.add(ArkComplianceObligation(entity_id=entity.id, **obligation))
    db.commit()
    request.session["active_entity_id"] = entity.id
    request.session["active_entity_slug"] = entity.slug
    request.session["active_entity_name"] = entity.name
    return _entity_out(entity, "admin", True, True)


@router.post("/entities/{slug}/activate")
def activate_entity(slug: str, request: Request, db: Session = Depends(get_control_db)):
    entity = db.query(ArkEntity).filter(ArkEntity.slug == slug).first()
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    _require_entity_access(db, request, entity.id)
    if entity.status == "archived":
        raise HTTPException(
            status_code=409, detail="Archived entity cannot be activated"
        )
    request.session["active_entity_id"] = entity.id
    request.session["active_entity_slug"] = entity.slug
    request.session["active_entity_name"] = entity.name
    access = _human_access(db, _principal(request)[1], entity.id)
    return _entity_out(
        entity,
        access.role if access else None,
        True,
        bool(access and access.protected_approver),
    )


@router.put("/entities/{entity_id}/access")
def grant_entity_access(
    entity_id: int,
    data: AccessGrant,
    request: Request,
    db: Session = Depends(get_control_db),
):
    entity = db.get(ArkEntity, entity_id)
    _require_entity_admin(db, request, entity_id)
    user = (
        db.query(User)
        .filter(User.username == data.username.strip().lower(), User.is_active)
        .first()
    )
    if entity is None or user is None:
        raise HTTPException(status_code=404, detail="Entity or active user not found")
    row = (
        db.query(ArkEntityAccess)
        .filter_by(entity_id=entity_id, user_id=user.id)
        .first()
    )
    if row is None:
        row = ArkEntityAccess(entity_id=entity_id, user_id=user.id)
        db.add(row)
    if data.protected_approver or bool(row.protected_approver):
        _require_protected_approver(db, request, entity_id)
    if row.protected_approver and not data.protected_approver:
        remaining = (
            db.query(ArkEntityAccess)
            .filter(
                ArkEntityAccess.entity_id == entity_id,
                ArkEntityAccess.protected_approver,
                ArkEntityAccess.user_id != user.id,
            )
            .count()
        )
        if remaining == 0:
            raise HTTPException(
                status_code=409, detail="Each entity requires a protected approver"
            )
    row.role = data.role
    row.is_default = data.is_default
    row.protected_approver = data.protected_approver
    if data.is_default:
        db.query(ArkEntityAccess).filter(
            ArkEntityAccess.user_id == user.id,
            ArkEntityAccess.entity_id != entity_id,
        ).update({ArkEntityAccess.is_default: False})
    db.commit()
    return {
        "entity_id": entity_id,
        "username": user.username,
        "role": row.role,
        "is_default": row.is_default,
        "protected_approver": row.protected_approver,
    }


@router.put("/entities/{entity_id}/governance")
def update_entity_governance(
    entity_id: int,
    data: EntityGovernanceUpdate,
    request: Request,
    db: Session = Depends(get_control_db),
):
    approver = _require_protected_approver(db, request, entity_id)
    entity = db.get(ArkEntity, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    if entity.status == "dormant" and data.posting_mode == "autopost_ordinary":
        raise HTTPException(
            status_code=409, detail="Dormant entities cannot enable autonomous posting"
        )
    entity.posting_mode = data.posting_mode
    entity.facts_status = data.facts_status
    entity.profile = data.profile
    db.commit()
    return {
        "entity_id": entity.id,
        "posting_mode": entity.posting_mode,
        "facts_status": entity.facts_status,
        "profile": entity.profile,
        "approved_by": approver,
    }


@router.put("/entities/{entity_id}/agent-grants")
def grant_agent(
    entity_id: int,
    data: AgentGrantIn,
    request: Request,
    db: Session = Depends(get_control_db),
):
    _require_protected_approver(db, request, entity_id)
    if db.get(ArkEntity, entity_id) is None or db.get(ApiToken, data.token_id) is None:
        raise HTTPException(status_code=404, detail="Entity or token not found")
    row = (
        db.query(ArkAgentGrant)
        .filter_by(entity_id=entity_id, token_id=data.token_id)
        .first()
    )
    if row is None:
        row = ArkAgentGrant(entity_id=entity_id, token_id=data.token_id)
        db.add(row)
    row.agent_role = data.agent_role
    row.model_id = data.model_id.strip()
    row.model_family = data.model_family.strip()
    row.prompt_version = data.prompt_version.strip()
    row.is_active = True
    db.commit()
    return {
        "entity_id": entity_id,
        "token_id": data.token_id,
        "agent_role": row.agent_role,
        "model_id": row.model_id,
        "model_family": row.model_family,
        "prompt_version": row.prompt_version,
        "is_active": True,
    }


@router.get("/entities/{entity_id}/evidence")
def list_evidence(
    entity_id: int, request: Request, db: Session = Depends(get_control_db)
):
    if db.get(ArkEntity, entity_id) is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    _require_entity_access(db, request, entity_id)
    rows = (
        db.query(ArkEvidence)
        .filter_by(entity_id=entity_id)
        .order_by(ArkEvidence.modified_at.desc())
        .limit(1000)
        .all()
    )
    return [
        {
            "id": row.id,
            "source_path": row.source_path,
            "category": row.category,
            "content_hash": row.content_hash,
            "size_bytes": row.size_bytes,
            "mime_type": row.mime_type,
            "status": row.status,
            "quarantine_reason": row.quarantine_reason,
            "modified_at": row.modified_at.isoformat() if row.modified_at else None,
            "facts": [
                {
                    "id": fact.id,
                    "key": fact.fact_key,
                    "value": fact.value,
                    "confidence": float(fact.confidence),
                    "locator": fact.locator,
                    "status": fact.status,
                    "source_hash": fact.source_hash,
                }
                for fact in db.query(ArkEvidenceFact)
                .filter_by(evidence_id=row.id)
                .order_by(ArkEvidenceFact.fact_key)
                .all()
            ],
        }
        for row in rows
    ]


@router.post("/entities/{entity_id}/evidence/scan")
def scan_evidence(
    entity_id: int, request: Request, db: Session = Depends(get_control_db)
):
    _require_entity_access(db, request, entity_id, write=True)
    entity = db.get(ArkEntity, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    try:
        return scan_and_extract_entity(db, entity)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/evidence/{evidence_id}")
def review_evidence(
    evidence_id: int,
    data: EvidenceReview,
    request: Request,
    db: Session = Depends(get_control_db),
):
    evidence = db.get(ArkEvidence, evidence_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    reviewer = _require_entity_access(db, request, evidence.entity_id, write=True)
    if data.status == "verified" and not evidence.content_hash:
        raise HTTPException(
            status_code=409,
            detail="Evidence must have a content hash before verification",
        )
    evidence.status = data.status
    if data.extracted_text_hash is not None:
        evidence.extracted_text_hash = data.extracted_text_hash
    if data.extractor_version is not None:
        evidence.extractor_version = data.extractor_version
    evidence.reviewed_by = reviewer
    evidence.reviewed_at = _now()
    facts = (
        db.query(ArkEvidenceFact)
        .filter(
            ArkEvidenceFact.evidence_id == evidence.id,
            ArkEvidenceFact.source_hash == evidence.content_hash,
        )
        .all()
    )
    for fact in facts:
        fact.status = data.status
        fact.reviewed_by = reviewer
        fact.reviewed_at = evidence.reviewed_at
    db.commit()
    return {"id": evidence.id, "status": evidence.status, "reviewed_by": reviewer}


@router.get("/entities/{entity_id}/candidates")
def list_candidates(
    entity_id: int, request: Request, db: Session = Depends(get_control_db)
):
    if db.get(ArkEntity, entity_id) is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    _require_entity_access(db, request, entity_id)
    rows = (
        db.query(ArkPostingCandidate)
        .filter_by(entity_id=entity_id)
        .order_by(ArkPostingCandidate.id.desc())
        .limit(500)
        .all()
    )
    return [_candidate_out(db, row) for row in rows]


@router.post("/entities/{entity_id}/candidates", status_code=201)
def create_candidate(
    entity_id: int,
    data: CandidateCreate,
    request: Request,
    db: Session = Depends(get_control_db),
):
    creator = _require_entity_access(db, request, entity_id, write=True)
    entity = db.get(ArkEntity, entity_id)
    evidence = db.get(ArkEvidence, data.evidence_id)
    if entity is None or evidence is None or evidence.entity_id != entity_id:
        raise HTTPException(status_code=404, detail="Entity or evidence not found")
    try:
        txn_date = datetime.strptime(data.transaction_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="transaction_date must be YYYY-MM-DD"
        ) from exc
    candidate = ArkPostingCandidate(
        entity_id=entity_id,
        evidence_id=evidence.id,
        transaction_date=txn_date,
        description=data.description.strip(),
        reference=(data.reference or "").strip() or None,
        currency=data.currency,
        lines=[line.model_dump(mode="json") for line in data.lines],
        tax_context=data.tax_context,
        action_type=data.action_type,
        status="blocked" if is_protected_action(data.action_type) else "reviewing",
        block_reason=PROTECTED_ACTIONS.get(data.action_type),
        created_by=creator,
    )
    db.add(candidate)
    db.flush()
    if is_protected_action(data.action_type):
        db.add(
            ArkProtectedAction(
                entity_id=entity_id,
                candidate_id=candidate.id,
                action_type=data.action_type,
                reason=PROTECTED_ACTIONS[data.action_type],
                requested_by=creator,
            )
        )
    db.commit()
    return _candidate_out(db, candidate)


@router.put("/candidates/{candidate_id}/decision")
def record_agent_decision(
    candidate_id: int,
    data: AgentDecisionIn,
    request: Request,
    db: Session = Depends(get_control_db),
):
    candidate = db.get(ArkPostingCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    kind, _name, _role = _principal(request)
    if kind != "token":
        raise HTTPException(
            status_code=403, detail="Agent decisions require a granted API token"
        )
    _require_entity_access(
        db, request, candidate.entity_id, write=True, agent_role=data.agent_role
    )
    _kind, label, _role = _principal(request)
    grant = _agent_access(db, label, candidate.entity_id)
    if (
        grant is None
        or data.model_id.strip() != grant.model_id
        or data.model_family.strip() != grant.model_family
        or data.prompt_version.strip() != grant.prompt_version
    ):
        raise HTTPException(
            status_code=409,
            detail="Decision model identity does not match the human-approved agent grant",
        )
    if candidate.status in ("posted", "rejected"):
        raise HTTPException(status_code=409, detail="Candidate is final")
    row = (
        db.query(ArkAgentDecision)
        .filter_by(candidate_id=candidate.id, agent_role=data.agent_role)
        .first()
    )
    if row is None:
        row = ArkAgentDecision(candidate_id=candidate.id, agent_role=data.agent_role)
        db.add(row)
    row.model_id = grant.model_id
    row.model_family = grant.model_family
    row.prompt_version = grant.prompt_version
    row.policy_version = POLICY_VERSION
    row.confidence = data.confidence
    row.decision = data.decision
    evidence = db.get(ArkEvidence, candidate.evidence_id)
    if evidence is None or evidence.status != "verified" or not evidence.content_hash:
        raise HTTPException(
            status_code=409,
            detail="Verified evidence is required before an agent decision",
        )
    row.evidence_hash = evidence.content_hash
    row.rationale_hash = sha256(data.rationale.encode("utf-8")).hexdigest()
    row.created_at = _now()
    context_hash = sha256(
        json.dumps(
            {
                "candidate_id": candidate.id,
                "evidence_hash": evidence.content_hash,
                "role": data.agent_role,
                "policy_version": POLICY_VERSION,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    response_hash = sha256(
        json.dumps(data.model_dump(mode="json"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    db.add(
        ArkAgentDecisionRun(
            candidate_id=candidate.id,
            agent_role=data.agent_role,
            model_id=grant.model_id,
            model_family=grant.model_family,
            prompt_version=grant.prompt_version,
            policy_version=POLICY_VERSION,
            confidence=data.confidence,
            decision=data.decision,
            evidence_hash=evidence.content_hash,
            context_hash=context_hash,
            rationale_hash=row.rationale_hash,
            response_hash=response_hash,
        )
    )
    candidate.status = "reviewing"
    db.commit()
    return _candidate_out(db, candidate)


def _candidate_and_access(
    candidate_id: int, request: Request, db: Session
) -> ArkPostingCandidate:
    candidate = db.get(ArkPostingCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    _require_entity_access(db, request, candidate.entity_id, write=True)
    return candidate


@router.post("/candidates/{candidate_id}/controller-run")
def run_controller_review(
    candidate_id: int,
    request: Request,
    control_db: Session = Depends(get_control_db),
    ledger_db: Session = Depends(get_db),
):
    candidate = _candidate_and_access(candidate_id, request, control_db)
    entity = control_db.get(ArkEntity, candidate.entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    if entity_mode_enabled() and ledger_db.bind.url.database != entity.database_name:
        raise HTTPException(
            status_code=409,
            detail="Activate the candidate's entity before agent review",
        )
    result = run_candidate_review(control_db, ledger_db, candidate)
    if result["status"] == "blocked":
        raise HTTPException(status_code=409, detail=result)
    return result


@router.get("/entities/{entity_id}/controller-runs")
def list_controller_runs(
    entity_id: int,
    request: Request,
    db: Session = Depends(get_control_db),
):
    if db.get(ArkEntity, entity_id) is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    _require_entity_access(db, request, entity_id)
    runs = (
        db.query(ArkControllerRun)
        .filter_by(entity_id=entity_id)
        .order_by(ArkControllerRun.id.desc())
        .limit(100)
        .all()
    )
    issues = (
        db.query(ArkControllerIssue)
        .filter_by(entity_id=entity_id)
        .order_by(ArkControllerIssue.id.desc())
        .limit(250)
        .all()
    )
    return {
        "runs": [
            {
                "id": row.id,
                "run_type": row.run_type,
                "status": row.status,
                "policy_version": row.policy_version,
                "summary": row.summary,
                "error_code": row.error_code,
                "started_at": row.started_at.isoformat(),
                "completed_at": (
                    row.completed_at.isoformat() if row.completed_at else None
                ),
            }
            for row in runs
        ],
        "issues": [
            {
                "id": row.id,
                "run_id": row.run_id,
                "category": row.category,
                "severity": row.severity,
                "status": row.status,
                "title": row.title,
                "detail": row.detail,
                "due_date": row.due_date.isoformat() if row.due_date else None,
            }
            for row in issues
        ],
    }


@router.post("/candidates/{candidate_id}/evaluate")
def evaluate_posting(
    candidate_id: int,
    request: Request,
    control_db: Session = Depends(get_control_db),
    ledger_db: Session = Depends(get_db),
):
    candidate = _candidate_and_access(candidate_id, request, control_db)
    result = evaluate_candidate(control_db, ledger_db, candidate)
    candidate.deterministic_checks = result
    candidate.status = "ready" if result["passed"] else "blocked"
    candidate.block_reason = None if result["passed"] else ", ".join(result["failed"])
    control_db.commit()
    return _candidate_out(control_db, candidate)


@router.post("/candidates/{candidate_id}/post")
def post_candidate(
    candidate_id: int,
    request: Request,
    control_db: Session = Depends(get_control_db),
    ledger_db: Session = Depends(get_db),
):
    candidate = _candidate_and_access(candidate_id, request, control_db)
    entity = control_db.get(ArkEntity, candidate.entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    if entity_mode_enabled() and ledger_db.bind.url.database != entity.database_name:
        raise HTTPException(
            status_code=409, detail="Activate the candidate's entity before posting"
        )

    if ledger_db.bind.dialect.name == "postgresql":
        # One posting attempt per evidence item across all app workers. The
        # ledger commit occurs before this lock releases, so a concurrent
        # candidate sees the existing source record instead of double-posting.
        ledger_db.execute(
            text("SELECT pg_advisory_xact_lock(:evidence_id)"),
            {"evidence_id": candidate.evidence_id},
        )
    existing = (
        ledger_db.query(Transaction)
        .filter_by(
            source_type=ARKCPA_LEDGER_SOURCE_TYPE,
            source_id=candidate.evidence_id,
        )
        .first()
    )
    if existing is not None:
        candidate.status = "posted"
        candidate.posted_transaction_id = existing.id
        control_db.commit()
        return _candidate_out(control_db, candidate)

    result = evaluate_candidate(control_db, ledger_db, candidate)
    candidate.deterministic_checks = result
    if not result["passed"]:
        candidate.status = "blocked"
        candidate.block_reason = ", ".join(result["failed"])
        control_db.commit()
        raise HTTPException(
            status_code=409, detail={"message": "Posting gate failed", **result}
        )

    candidate.status = "posting"
    candidate.block_reason = None
    control_db.commit()
    try:
        txn = create_journal_entry(
            ledger_db,
            candidate.transaction_date,
            candidate.description,
            candidate.lines,
            source_type=ARKCPA_LEDGER_SOURCE_TYPE,
            source_id=candidate.evidence_id,
            reference=candidate.reference,
        )
        ledger_db.commit()
        ledger_db.refresh(txn)
    except Exception:
        ledger_db.rollback()
        candidate.status = "blocked"
        candidate.block_reason = "ledger posting failed; no entry committed"
        control_db.commit()
        raise
    candidate.status = "posted"
    candidate.posted_transaction_id = txn.id
    control_db.commit()
    return _candidate_out(control_db, candidate)


@router.get("/entities/{entity_id}/obligations")
def list_obligations(
    entity_id: int, request: Request, db: Session = Depends(get_control_db)
):
    if db.get(ArkEntity, entity_id) is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    _require_entity_access(db, request, entity_id)
    rows = (
        db.query(ArkComplianceObligation)
        .filter_by(entity_id=entity_id)
        .order_by(ArkComplianceObligation.tax_year.desc(), ArkComplianceObligation.code)
        .all()
    )
    return [
        {
            "id": row.id,
            "code": row.code,
            "label": row.label,
            "authority": row.authority,
            "tax_year": row.tax_year,
            "due_date": row.due_date.isoformat() if row.due_date else None,
            "status": row.status,
            "human_required": row.human_required,
        }
        for row in rows
    ]


@router.get("/protected-actions")
def list_protected_actions(request: Request, db: Session = Depends(get_control_db)):
    entities = list_entities(request, db)
    ids = [row["id"] for row in entities]
    approver_ids = {
        row["id"] for row in entities if row.get("protected_approver") is True
    }
    if not ids:
        return []
    rows = (
        db.query(ArkProtectedAction)
        .filter(ArkProtectedAction.entity_id.in_(ids))
        .order_by(ArkProtectedAction.id.desc())
        .limit(500)
        .all()
    )
    return [
        {
            "id": row.id,
            "entity_id": row.entity_id,
            "candidate_id": row.candidate_id,
            "action_type": row.action_type,
            "status": row.status,
            "reason": row.reason,
            "requested_by": row.requested_by,
            "approved_by": row.approved_by,
            "approval_note": row.approval_note,
            "approved_at": row.approved_at.isoformat() if row.approved_at else None,
            "can_approve": row.entity_id in approver_ids and row.status == "required",
        }
        for row in rows
    ]


@router.put("/protected-actions/{action_id}/decision")
def decide_protected_action(
    action_id: int,
    data: ProtectedDecisionIn,
    request: Request,
    db: Session = Depends(get_control_db),
):
    action = db.get(ArkProtectedAction, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Protected action not found")
    approver = _require_protected_approver(db, request, action.entity_id)
    if action.status != "required":
        raise HTTPException(status_code=409, detail="Protected action already decided")
    action.status = data.decision
    action.approved_by = approver
    action.approved_at = _now()
    action.approval_note = data.note.strip()
    action.decision_hash = sha256(
        json.dumps(
            {
                "action_id": action.id,
                "entity_id": action.entity_id,
                "decision": data.decision,
                "note": action.approval_note,
                "approver": approver,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    db.commit()
    return {
        "id": action.id,
        "status": action.status,
        "approved_by": action.approved_by,
        "approved_at": action.approved_at.isoformat(),
        "decision_hash": action.decision_hash,
        "execution_performed": False,
    }


@router.get("/authority-sources")
def list_authority_sources(request: Request, db: Session = Depends(get_control_db)):
    name = _require_human(request)
    membership = (
        db.query(ArkEntityAccess.id)
        .join(User, User.id == ArkEntityAccess.user_id)
        .filter(User.username == name, User.is_active)
        .first()
    )
    if membership is None:
        raise HTTPException(status_code=403, detail="Ark CPA entity access required")
    seed_authority_sources(db)
    sources = db.query(ArkAuthoritySource).order_by(ArkAuthoritySource.code).all()
    proposals = (
        db.query(ArkRuleProposal).order_by(ArkRuleProposal.id.desc()).limit(250).all()
    )
    return {
        "automatic_policy_change": False,
        "sources": [
            {
                "id": row.id,
                "code": row.code,
                "title": row.title,
                "jurisdiction": row.jurisdiction,
                "authority_level": row.authority_level,
                "url": row.url,
                "enabled": row.enabled,
                "last_checked_at": (
                    row.last_checked_at.isoformat() if row.last_checked_at else None
                ),
                "current_hash": (
                    db.query(ArkSourceSnapshot.content_hash)
                    .filter_by(source_id=row.id, status="current")
                    .order_by(ArkSourceSnapshot.retrieved_at.desc())
                    .scalar()
                ),
            }
            for row in sources
        ],
        "proposals": [
            {
                "id": row.id,
                "code": row.code,
                "title": row.title,
                "status": row.status,
                "proposed_change": row.proposed_change,
                "test_results": row.test_results,
                "approved_by": row.approved_by,
            }
            for row in proposals
        ],
    }


@router.post("/authority-sources/refresh")
def refresh_authority_source_registry(
    request: Request, db: Session = Depends(get_control_db)
):
    requested_by = _require_any_protected_approver(db, request)
    return {
        "requested_by": requested_by,
        "automatic_policy_change": False,
        "results": refresh_all_authority_sources(db),
    }


@router.get("/entities/{entity_id}/workpapers")
def list_workpapers(
    entity_id: int, request: Request, db: Session = Depends(get_control_db)
):
    if db.get(ArkEntity, entity_id) is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    _require_entity_access(db, request, entity_id)
    rows = (
        db.query(ArkWorkpaperPackage)
        .filter_by(entity_id=entity_id)
        .order_by(ArkWorkpaperPackage.id.desc())
        .all()
    )
    return [
        {
            "id": row.id,
            "obligation_id": row.obligation_id,
            "tax_year": row.tax_year,
            "status": row.status,
            "manifest": row.manifest,
            "package_hash": row.package_hash,
            "disclaimer": row.disclaimer,
            "created_by": row.created_by,
            "approved_by": row.approved_by,
        }
        for row in rows
    ]


@router.post("/entities/{entity_id}/workpapers", status_code=201)
def create_workpaper(
    entity_id: int,
    data: WorkpaperCreate,
    request: Request,
    db: Session = Depends(get_control_db),
):
    creator = _require_entity_access(db, request, entity_id, write=True)
    if data.obligation_id is not None:
        obligation = db.get(ArkComplianceObligation, data.obligation_id)
        if obligation is None or obligation.entity_id != entity_id:
            raise HTTPException(
                status_code=404, detail="Obligation not found for entity"
            )
    evidence = (
        db.query(ArkEvidence)
        .filter(ArkEvidence.id.in_(data.evidence_ids or [-1]))
        .order_by(ArkEvidence.id)
        .all()
    )
    if len(evidence) != len(set(data.evidence_ids)) or any(
        row.entity_id != entity_id or row.status != "verified" or not row.content_hash
        for row in evidence
    ):
        raise HTTPException(
            status_code=409,
            detail="Workpapers may contain only verified evidence from this entity",
        )
    manifest = {
        "entity_id": entity_id,
        "obligation_id": data.obligation_id,
        "tax_year": data.tax_year,
        "evidence": [
            {"id": row.id, "sha256": row.content_hash, "source_path": row.source_path}
            for row in evidence
        ],
        "submission_performed": False,
    }
    package_hash = sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    row = ArkWorkpaperPackage(
        entity_id=entity_id,
        obligation_id=data.obligation_id,
        tax_year=data.tax_year,
        status="draft",
        manifest=manifest,
        package_hash=package_hash,
        created_by=creator,
    )
    db.add(row)
    db.commit()
    return {
        "id": row.id,
        "status": row.status,
        "package_hash": row.package_hash,
        "submission_performed": False,
    }
