"""Unified Ark CPA entity, evidence, agent-review, and compliance API."""

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
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
    ArkAgentGrant,
    ArkComplianceObligation,
    ArkEntity,
    ArkEntityAccess,
    ArkEvidence,
    ArkPostingCandidate,
    ArkProtectedAction,
)
from app.models.transactions import Transaction
from app.models.users import User
from app.schemas.common import StrictModel
from app.services.accounting import create_journal_entry
from app.services.ark_files import scan_entity_files
from app.services.arkcpa_policy import evaluate_candidate
from app.services.arkcpa_rules import (
    ENTITY_TYPES,
    MIN_AUTONOMOUS_CONFIDENCE,
    OBLIGATION_PACKS,
    POLICY_VERSION,
    PROTECTED_ACTIONS,
    is_protected_action,
    obligations_for,
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


def _require_admin(request: Request) -> str:
    kind, name, role = _principal(request)
    if kind != "user" or role != "admin":
        raise HTTPException(status_code=403, detail="Human admin required")
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
    entity: ArkEntity, role: str | None = None, active: bool = False
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
        "currency": entity.currency,
        "fiscal_year_end": f"{entity.fiscal_year_end_month:02d}-{entity.fiscal_year_end_day:02d}",
        "ark_files_path": entity.ark_files_path,
        "payroll_mode": (
            "evidence-only" if not entity.payroll_enabled else "human-controlled"
        ),
        "access_role": role,
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


@router.get("/status")
def arkcpa_status(request: Request, db: Session = Depends(get_control_db)):
    entities = list_entities(request, db)
    entity_ids = [row["id"] for row in entities]
    counts = {
        "evidence": 0,
        "review_queue": 0,
        "protected_actions": 0,
        "obligations": 0,
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
        }
    return {
        "product": "Ark CPA",
        "entity_mode": entity_mode_enabled(),
        "policy_version": POLICY_VERSION,
        "autonomous_confidence_threshold": MIN_AUTONOMOUS_CONFIDENCE,
        "raw_documents_in_model_memory": False,
        "cloud_fallback": "redacted-only",
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
            db.query(ArkEntity, ArkAgentGrant.agent_role)
            .join(ArkAgentGrant, ArkAgentGrant.entity_id == ArkEntity.id)
            .join(ApiToken, ApiToken.id == ArkAgentGrant.token_id)
            .filter(ApiToken.label == name, ApiToken.is_active, ArkAgentGrant.is_active)
            .order_by(ArkEntity.name)
            .all()
        )
    else:
        rows = (
            db.query(ArkEntity, ArkEntityAccess.role)
            .join(ArkEntityAccess, ArkEntityAccess.entity_id == ArkEntity.id)
            .join(User, User.id == ArkEntityAccess.user_id)
            .filter(User.username == name, User.is_active)
            .order_by(ArkEntity.name)
            .all()
        )
    return [
        _entity_out(entity, role, entity.slug == active_slug) for entity, role in rows
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
        )
    )
    for obligation in obligations_for(entity.entity_type):
        db.add(ArkComplianceObligation(entity_id=entity.id, **obligation))
    db.commit()
    request.session["active_entity_id"] = entity.id
    request.session["active_entity_slug"] = entity.slug
    request.session["active_entity_name"] = entity.name
    return _entity_out(entity, "admin", True)


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
    return _entity_out(entity, active=True)


@router.put("/entities/{entity_id}/access")
def grant_entity_access(
    entity_id: int,
    data: AccessGrant,
    request: Request,
    db: Session = Depends(get_control_db),
):
    _require_admin(request)
    entity = db.get(ArkEntity, entity_id)
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
    row.role = data.role
    row.is_default = data.is_default
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
    }


@router.put("/entities/{entity_id}/agent-grants")
def grant_agent(
    entity_id: int,
    data: AgentGrantIn,
    request: Request,
    db: Session = Depends(get_control_db),
):
    _require_admin(request)
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
        return scan_entity_files(db, entity)
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
    evidence.extracted_text_hash = data.extracted_text_hash
    evidence.extractor_version = data.extractor_version
    evidence.reviewed_by = reviewer
    evidence.reviewed_at = _now()
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
        # One posting attempt per candidate across all app workers. The lock is
        # held by the ledger transaction and releases on commit or rollback.
        ledger_db.execute(
            text("SELECT pg_advisory_xact_lock(:candidate_id)"),
            {"candidate_id": candidate.id},
        )
    existing = (
        ledger_db.query(Transaction)
        .filter_by(source_type="arkcpa_agent", source_id=candidate.id)
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
            source_type="arkcpa_agent",
            source_id=candidate.id,
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
        }
        for row in rows
    ]
