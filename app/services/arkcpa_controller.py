"""Independent Ark Bookkeeper and Ark Controller review orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from datetime import date
from urllib.parse import urlparse

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.accounts import Account
from app.models.arkcpa import (
    ArkAgentDecision,
    ArkAgentDecisionRun,
    ArkControllerIssue,
    ArkControllerRun,
    ArkEntity,
    ArkEvidence,
    ArkEvidenceFact,
    ArkPostingCandidate,
)
from app.models.transactions import Transaction
from app.services.accounting import create_journal_entry
from app.services.arkcpa_agents import (
    PROMPT_VERSION,
    build_agent_context,
    context_fingerprint,
    system_prompt,
)
from app.services.arkcpa_policy import evaluate_candidate
from app.services.arkcpa_rules import POLICY_VERSION

MAX_MODEL_RESPONSE_BYTES = 256 * 1024
ARKCPA_LEDGER_SOURCE_TYPE = "arkcpa_evidence"
_AGENT_PROFILE_KEYS = {
    "business_activity",
    "entity_classification",
    "fiscal_status",
    "operating_status",
    "revenue_status",
    "tax_registration_status",
}

BOOKKEEPER_PROPOSAL_PROMPT = """You are Ark Bookkeeper. From only the verified,
structured evidence facts and the selected entity chart, propose one ordinary
journal candidate. Never infer missing amounts, tax treatment, accounts, dates,
or entity facts. If the evidence is insufficient, return no_candidate=true and
say why. You cannot file, move money, change credentials, close a period, or run
payroll. Return only JSON with exactly: no_candidate, candidate, confidence,
rationale. A candidate has exactly: transaction_date, description, reference,
currency, lines, tax_context. Each line has account_id, debit, credit,
description."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ControllerConfig:
    base_url: str
    api_key: str
    bookkeeper_model: str
    bookkeeper_family: str
    controller_model: str
    controller_family: str
    timeout_seconds: float = 60.0
    require_gateway_family_proof: bool = True

    @classmethod
    def from_env(cls) -> "ControllerConfig":
        base_url = os.getenv("ARKCPA_AI_BASE_URL", "").strip().rstrip("/")
        if not base_url:
            raise ValueError("ARKCPA_AI_BASE_URL is not configured")
        return cls(
            base_url=base_url,
            api_key=os.getenv("ARKCPA_AI_API_KEY", "").strip(),
            bookkeeper_model=os.getenv("ARKCPA_BOOKKEEPER_MODEL", "ark-coder-oss").strip(),
            bookkeeper_family=os.getenv("ARKCPA_BOOKKEEPER_FAMILY", "gpt-oss").strip(),
            controller_model=os.getenv("ARKCPA_CONTROLLER_MODEL", "ark-brain").strip(),
            controller_family=os.getenv(
                "ARKCPA_CONTROLLER_FAMILY", "nemotron3-super"
            ).strip(),
            timeout_seconds=float(os.getenv("ARKCPA_AI_TIMEOUT_SECONDS", "60")),
            require_gateway_family_proof=os.getenv(
                "ARKCPA_REQUIRE_GATEWAY_FAMILY_PROOF", "true"
            ).lower()
            in {"1", "true", "yes"},
        )

    def validate(self) -> None:
        parsed = urlparse(self.base_url)
        allowed = {
            item.strip().lower()
            for item in os.getenv(
                "ARKCPA_AI_ALLOWED_HOSTS",
                "litellm,host.docker.internal,127.0.0.1,localhost",
            ).split(",")
            if item.strip()
        }
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Ark CPA AI gateway URL must be HTTP(S)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Ark CPA AI gateway URL contains forbidden components")
        if parsed.hostname.lower() not in allowed:
            raise ValueError("Ark CPA AI gateway host is not allowlisted")
        if not self.bookkeeper_model or not self.controller_model:
            raise ValueError("Both Ark CPA model routes must be configured")
        if not self.bookkeeper_family or not self.controller_family:
            raise ValueError("Both Ark CPA model families must be configured")
        if self.bookkeeper_family.lower() == self.controller_family.lower():
            raise ValueError("Bookkeeper and Controller must use independent model families")
        if not 1 <= self.timeout_seconds <= 180:
            raise ValueError("Ark CPA AI timeout must be between 1 and 180 seconds")


def _decision_json(raw: str) -> dict:
    if len(raw.encode("utf-8")) > MAX_MODEL_RESPONSE_BYTES:
        raise ValueError("Agent response exceeded the bounded response size")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Agent did not return valid JSON") from exc
    required = {"decision", "confidence", "rationale", "checks"}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("Agent response does not match the governed decision schema")
    if data["decision"] not in {"approve", "reject", "escalate"}:
        raise ValueError("Agent returned an invalid decision")
    if not isinstance(data["rationale"], str) or not 1 <= len(data["rationale"]) <= 20000:
        raise ValueError("Agent rationale is missing or too long")
    if not isinstance(data["checks"], dict):
        raise ValueError("Agent checks must be an object")
    if isinstance(data["confidence"], bool):
        raise ValueError("Agent confidence must be numeric")
    try:
        confidence = Decimal(str(data["confidence"]))
    except Exception as exc:
        raise ValueError("Agent confidence must be numeric") from exc
    if confidence < 0 or confidence > 1:
        raise ValueError("Agent confidence must be between zero and one")
    data["confidence"] = confidence
    return data


def _proposal_json(raw: str) -> dict:
    if len(raw.encode("utf-8")) > MAX_MODEL_RESPONSE_BYTES:
        raise ValueError("Bookkeeper proposal exceeded the bounded response size")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Bookkeeper did not return valid JSON") from exc
    if not isinstance(data, dict) or set(data) != {
        "no_candidate",
        "candidate",
        "confidence",
        "rationale",
    }:
        raise ValueError("Bookkeeper response does not match the proposal schema")
    if not isinstance(data["no_candidate"], bool):
        raise ValueError("Bookkeeper no_candidate flag must be boolean")
    if not isinstance(data["rationale"], str) or not 1 <= len(data["rationale"]) <= 20000:
        raise ValueError("Bookkeeper rationale is missing or too long")
    try:
        confidence = Decimal(str(data["confidence"]))
    except Exception as exc:
        raise ValueError("Bookkeeper confidence must be numeric") from exc
    if confidence < 0 or confidence > 1:
        raise ValueError("Bookkeeper confidence must be between zero and one")
    data["confidence"] = confidence
    if data["no_candidate"]:
        if data["candidate"] is not None:
            raise ValueError("No-candidate responses must use candidate=null")
        return data
    candidate = data["candidate"]
    required = {
        "transaction_date",
        "description",
        "reference",
        "currency",
        "lines",
        "tax_context",
    }
    if not isinstance(candidate, dict) or set(candidate) != required:
        raise ValueError("Bookkeeper candidate does not match the governed schema")
    try:
        date.fromisoformat(candidate["transaction_date"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Bookkeeper candidate date must be YYYY-MM-DD") from exc
    if not isinstance(candidate["description"], str) or not candidate["description"].strip():
        raise ValueError("Bookkeeper candidate description is required")
    if len(candidate["description"]) > 1000:
        raise ValueError("Bookkeeper candidate description is too long")
    if candidate["reference"] is not None and (
        not isinstance(candidate["reference"], str) or len(candidate["reference"]) > 100
    ):
        raise ValueError("Bookkeeper candidate reference is invalid")
    if candidate["currency"] not in {"CAD", "USD"}:
        raise ValueError("Bookkeeper candidate currency is invalid")
    if not isinstance(candidate["tax_context"], dict):
        raise ValueError("Bookkeeper tax context must be an object")
    if not isinstance(candidate["lines"], list) or not 2 <= len(candidate["lines"]) <= 200:
        raise ValueError("Bookkeeper candidate must contain two to 200 lines")
    clean_lines = []
    for line in candidate["lines"]:
        if not isinstance(line, dict) or set(line) != {
            "account_id",
            "debit",
            "credit",
            "description",
        }:
            raise ValueError("Bookkeeper line does not match the governed schema")
        try:
            account_id = int(line["account_id"])
            debit = Decimal(str(line["debit"] or 0)).quantize(Decimal("0.01"))
            credit = Decimal(str(line["credit"] or 0)).quantize(Decimal("0.01"))
        except Exception as exc:
            raise ValueError("Bookkeeper line amounts or account are invalid") from exc
        if account_id < 1 or debit < 0 or credit < 0 or (debit > 0) == (credit > 0):
            raise ValueError("Bookkeeper line must have one positive debit or credit")
        description = line["description"]
        if not isinstance(description, str) or len(description) > 300:
            raise ValueError("Bookkeeper line description is invalid")
        clean_lines.append(
            {
                "account_id": account_id,
                "debit": str(debit),
                "credit": str(credit),
                "description": description,
            }
        )
    candidate["lines"] = clean_lines
    return data


def _assistant_envelope(
    response: httpx.Response,
    *,
    expected_family: str,
    require_family_proof: bool,
) -> tuple[str, dict]:
    if response.status_code != 200:
        raise ValueError(f"Ark AI gateway returned HTTP {response.status_code}")
    if len(response.content) > MAX_MODEL_RESPONSE_BYTES:
        raise ValueError("Ark AI gateway response exceeded the size limit")
    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ValueError("Ark AI gateway returned a malformed completion") from exc
    returned_model = body.get("model")
    provider_fields = body.get("provider_specific_fields") or {}
    returned_family = (
        response.headers.get("x-ark-model-family")
        or body.get("model_family")
        or (
            provider_fields.get("model_family")
            if isinstance(provider_fields, dict)
            else None
        )
    )
    if not isinstance(returned_model, str) or not returned_model.strip():
        raise ValueError("Ark AI gateway did not attest the resolved model")
    if require_family_proof and (
        not isinstance(returned_family, str)
        or returned_family.strip().lower() != expected_family.strip().lower()
    ):
        raise ValueError("Ark AI gateway did not attest the expected model family")
    return content, {
        "model": returned_model.strip(),
        "family": returned_family.strip() if isinstance(returned_family, str) else expected_family,
    }


def call_agent(
    config: ControllerConfig,
    *,
    role: str,
    model: str,
    context: dict,
    client: httpx.Client | None = None,
) -> tuple[dict, str, dict]:
    config.validate()
    url = f"{config.base_url}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt(role)},
            {
                "role": "user",
                "content": json.dumps(context, sort_keys=True, separators=(",", ":")),
            },
        ],
        "temperature": 0,
        "seed": 42,
        "max_tokens": 1200,
    }
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=config.timeout_seconds,
            verify=True,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "ark-cpa-controller/1"},
        )
    try:
        response = client.post(url, headers=headers, json=payload)
        raw, identity = _assistant_envelope(
            response,
            expected_family=(
                config.bookkeeper_family if role == "bookkeeper" else config.controller_family
            ),
            require_family_proof=config.require_gateway_family_proof,
        )
        return _decision_json(raw), sha256(response.content).hexdigest(), identity
    except httpx.HTTPError as exc:
        raise ValueError("Ark AI gateway request failed") from exc
    finally:
        if owns_client:
            client.close()


def call_bookkeeper_proposal(
    config: ControllerConfig,
    *,
    context: dict,
    client: httpx.Client | None = None,
) -> tuple[dict, str, dict]:
    config.validate()
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    payload = {
        "model": config.bookkeeper_model,
        "messages": [
            {"role": "system", "content": BOOKKEEPER_PROPOSAL_PROMPT},
            {"role": "user", "content": json.dumps(context, sort_keys=True, separators=(",", ":"))},
        ],
        "temperature": 0,
        "seed": 42,
        "max_tokens": 1600,
    }
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=config.timeout_seconds,
            verify=True,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "ark-cpa-controller/1"},
        )
    try:
        response = client.post(
            f"{config.base_url}/chat/completions", headers=headers, json=payload
        )
        raw, identity = _assistant_envelope(
            response,
            expected_family=config.bookkeeper_family,
            require_family_proof=config.require_gateway_family_proof,
        )
        return _proposal_json(raw), sha256(response.content).hexdigest(), identity
    except httpx.HTTPError as exc:
        raise ValueError("Ark AI gateway request failed") from exc
    finally:
        if owns_client:
            client.close()


def _entity_context(entity: ArkEntity) -> dict:
    profile = entity.profile if isinstance(entity.profile, dict) else {}
    return {
        "entity_ref": sha256(f"{entity.id}:{entity.slug}".encode("utf-8")).hexdigest()[:16],
        "entity_type": entity.entity_type,
        "jurisdiction": entity.jurisdiction,
        "currency": entity.currency,
        "fiscal_year_end": f"{entity.fiscal_year_end_month:02d}-{entity.fiscal_year_end_day:02d}",
        "status": entity.status,
        "facts_status": entity.facts_status,
        "profile": {
            key: profile[key]
            for key in sorted(_AGENT_PROFILE_KEYS)
            if key in profile
        },
    }


def _evidence_facts(control_db: Session, evidence: ArkEvidence) -> dict:
    rows = (
        control_db.query(ArkEvidenceFact)
        .filter(
            ArkEvidenceFact.evidence_id == evidence.id,
            ArkEvidenceFact.status == "verified",
            ArkEvidenceFact.source_hash == evidence.content_hash,
        )
        .order_by(ArkEvidenceFact.fact_key, ArkEvidenceFact.locator)
        .all()
    )
    return {
        row.fact_key: {
            "value": row.value,
            "confidence": float(row.confidence),
            "locator": row.locator,
        }
        for row in rows
    }


def _chart(ledger_db: Session) -> list[dict]:
    return [
        {
            "account_id": row.id,
            "name": row.name,
            "type": row.account_type.value,
            "active": bool(row.is_active),
        }
        for row in ledger_db.query(Account).filter(Account.is_active).order_by(Account.id).all()
    ]


def _candidate_context(candidate: ArkPostingCandidate) -> dict:
    return {
        "id": candidate.id,
        "transaction_date": candidate.transaction_date.isoformat(),
        "description": candidate.description,
        "reference": candidate.reference,
        "currency": candidate.currency,
        "lines": candidate.lines,
        "tax_context": candidate.tax_context,
        "action_type": candidate.action_type,
    }


def _record_decision(
    db: Session,
    *,
    run: ArkControllerRun,
    candidate: ArkPostingCandidate,
    role: str,
    model: str,
    family: str,
    evidence_hash: str,
    context_hash: str,
    result: dict,
    response_hash: str,
) -> None:
    rationale_hash = sha256(result["rationale"].encode("utf-8")).hexdigest()
    db.add(
        ArkAgentDecisionRun(
            candidate_id=candidate.id,
            controller_run_id=run.id,
            agent_role=role,
            model_id=model,
            model_family=family,
            prompt_version=PROMPT_VERSION,
            policy_version=POLICY_VERSION,
            confidence=result["confidence"],
            decision=result["decision"],
            evidence_hash=evidence_hash,
            context_hash=context_hash,
            rationale_hash=rationale_hash,
            response_hash=response_hash,
        )
    )
    current = (
        db.query(ArkAgentDecision)
        .filter_by(candidate_id=candidate.id, agent_role=role)
        .first()
    )
    if current is None:
        current = ArkAgentDecision(candidate_id=candidate.id, agent_role=role)
        db.add(current)
    current.model_id = model
    current.model_family = family
    current.prompt_version = PROMPT_VERSION
    current.policy_version = POLICY_VERSION
    current.confidence = result["confidence"]
    current.decision = result["decision"]
    current.evidence_hash = evidence_hash
    current.rationale_hash = rationale_hash
    current.created_at = _now()


def _block_run(
    db: Session,
    run: ArkControllerRun,
    entity_id: int,
    code: str,
    detail: str,
) -> dict:
    run.status = "blocked"
    run.error_code = code
    run.summary = {"outcome": "blocked", "reason": detail}
    run.completed_at = _now()
    db.add(
        ArkControllerIssue(
            run_id=run.id,
            entity_id=entity_id,
            category="agent_review",
            severity="high",
            title="Controller review blocked",
            detail=detail[:2000],
        )
    )
    db.commit()
    return {"run_id": run.id, "status": run.status, "error_code": code}


def run_candidate_review(
    control_db: Session,
    ledger_db: Session,
    candidate: ArkPostingCandidate,
    *,
    config: ControllerConfig | None = None,
    client: httpx.Client | None = None,
) -> dict:
    run = ArkControllerRun(
        entity_id=candidate.entity_id,
        run_type="candidate",
        status="running",
        policy_version=POLICY_VERSION,
    )
    control_db.add(run)
    control_db.commit()
    entity = control_db.get(ArkEntity, candidate.entity_id)
    evidence = control_db.get(ArkEvidence, candidate.evidence_id)
    if entity is None or evidence is None:
        return _block_run(control_db, run, candidate.entity_id, "missing_context", "Entity or evidence is missing")
    if evidence.status != "verified" or not evidence.content_hash:
        return _block_run(
            control_db,
            run,
            candidate.entity_id,
            "evidence_not_verified",
            "Human-verified evidence is required before agent review",
        )
    facts = _evidence_facts(control_db, evidence)
    if not facts:
        return _block_run(
            control_db,
            run,
            candidate.entity_id,
            "facts_not_verified",
            "Human-verified structured facts are required before agent review",
        )
    try:
        config = config or ControllerConfig.from_env()
        config.validate()
        chart = _chart(ledger_db)
        proposal = _candidate_context(candidate)
        bookkeeper_context = build_agent_context(
            agent_role="bookkeeper",
            entity=_entity_context(entity),
            evidence_hash=evidence.content_hash,
            extracted_facts=facts,
            chart=chart,
            candidate=proposal,
        )
        bookkeeper, bookkeeper_response_hash, bookkeeper_identity = call_agent(
            config,
            role="bookkeeper",
            model=config.bookkeeper_model,
            context=bookkeeper_context,
            client=client,
        )
        bookkeeper_context_hash = context_fingerprint(bookkeeper_context)
        _record_decision(
            control_db,
            run=run,
            candidate=candidate,
            role="bookkeeper",
            model=bookkeeper_identity["model"],
            family=bookkeeper_identity["family"],
            evidence_hash=evidence.content_hash,
            context_hash=bookkeeper_context_hash,
            result=bookkeeper,
            response_hash=bookkeeper_response_hash,
        )
        control_db.flush()
        controller_context = build_agent_context(
            agent_role="controller",
            entity=_entity_context(entity),
            evidence_hash=evidence.content_hash,
            extracted_facts=facts,
            chart=chart,
            candidate=proposal,
        )
        controller, controller_response_hash, controller_identity = call_agent(
            config,
            role="controller",
            model=config.controller_model,
            context=controller_context,
            client=client,
        )
        _record_decision(
            control_db,
            run=run,
            candidate=candidate,
            role="controller",
            model=controller_identity["model"],
            family=controller_identity["family"],
            evidence_hash=evidence.content_hash,
            context_hash=context_fingerprint(controller_context),
            result=controller,
            response_hash=controller_response_hash,
        )
        control_db.flush()
        gate = evaluate_candidate(
            control_db,
            ledger_db,
            candidate,
            autonomous=entity.posting_mode == "autopost_ordinary",
        )
        candidate.deterministic_checks = gate
        candidate.status = "ready" if gate["passed"] else "blocked"
        candidate.block_reason = None if gate["passed"] else ", ".join(gate["failed"])
        run.status = "completed"
        run.summary = {
            "candidate_id": candidate.id,
            "bookkeeper": {
                "decision": bookkeeper["decision"],
                "confidence": float(bookkeeper["confidence"]),
                "family": bookkeeper_identity["family"],
            },
            "controller": {
                "decision": controller["decision"],
                "confidence": float(controller["confidence"]),
                "family": controller_identity["family"],
            },
            "gate_passed": gate["passed"],
            "failed_checks": gate["failed"],
            "autonomous": gate["autonomous"],
        }
        run.completed_at = _now()
        control_db.commit()
        return {"run_id": run.id, "status": run.status, **run.summary}
    except ValueError as exc:
        control_db.rollback()
        run = control_db.get(ArkControllerRun, run.id)
        return _block_run(
            control_db,
            run,
            candidate.entity_id,
            "agent_contract_failed",
            str(exc),
        )
    except Exception:
        control_db.rollback()
        run = control_db.get(ArkControllerRun, run.id)
        return _block_run(
            control_db,
            run,
            candidate.entity_id,
            "controller_internal_error",
            "Controller review failed without persisting a posting decision",
        )


def run_evidence_bookkeeping(
    control_db: Session,
    ledger_db: Session,
    evidence: ArkEvidence,
    *,
    run_type: str = "daily",
    config: ControllerConfig | None = None,
    client: httpx.Client | None = None,
) -> dict:
    """Draft one candidate from verified facts, then independently review it."""
    existing = (
        control_db.query(ArkPostingCandidate)
        .filter(
            ArkPostingCandidate.evidence_id == evidence.id,
            ArkPostingCandidate.status != "rejected",
        )
        .order_by(ArkPostingCandidate.id.desc())
        .first()
    )
    if existing:
        return {
            "status": "skipped",
            "reason": "candidate_exists",
            "candidate_id": existing.id,
        }
    run = ArkControllerRun(
        entity_id=evidence.entity_id,
        run_type=run_type,
        status="running",
        policy_version=POLICY_VERSION,
    )
    control_db.add(run)
    control_db.commit()
    entity = control_db.get(ArkEntity, evidence.entity_id)
    if entity is None or evidence.status != "verified" or not evidence.content_hash:
        return _block_run(
            control_db,
            run,
            evidence.entity_id,
            "evidence_not_verified",
            "Human-verified evidence is required before bookkeeping",
        )
    facts = _evidence_facts(control_db, evidence)
    if not facts:
        return _block_run(
            control_db,
            run,
            evidence.entity_id,
            "facts_not_verified",
            "Human-verified structured facts are required before bookkeeping",
        )
    try:
        config = config or ControllerConfig.from_env()
        config.validate()
        chart = _chart(ledger_db)
        bookkeeper_context = build_agent_context(
            agent_role="bookkeeper",
            entity=_entity_context(entity),
            evidence_hash=evidence.content_hash,
            extracted_facts=facts,
            chart=chart,
        )
        proposal, proposal_response_hash, bookkeeper_identity = call_bookkeeper_proposal(
            config, context=bookkeeper_context, client=client
        )
        if proposal["no_candidate"]:
            run.status = "completed"
            run.summary = {
                "outcome": "no_candidate",
                "confidence": float(proposal["confidence"]),
                "rationale_hash": sha256(proposal["rationale"].encode()).hexdigest(),
            }
            run.completed_at = _now()
            control_db.commit()
            return {"run_id": run.id, "status": run.status, **run.summary}
        proposed = proposal["candidate"]
        if proposed["currency"] != entity.currency:
            raise ValueError("Bookkeeper candidate currency does not match the entity")
        candidate = ArkPostingCandidate(
            entity_id=entity.id,
            evidence_id=evidence.id,
            transaction_date=date.fromisoformat(proposed["transaction_date"]),
            description=proposed["description"].strip(),
            reference=(proposed["reference"] or "").strip() or None,
            currency=proposed["currency"],
            lines=proposed["lines"],
            tax_context=proposed["tax_context"],
            action_type="ordinary_entry",
            status="reviewing",
            deterministic_checks={},
            created_by="ark-bookkeeper",
        )
        control_db.add(candidate)
        control_db.flush()
        _record_decision(
            control_db,
            run=run,
            candidate=candidate,
            role="bookkeeper",
            model=bookkeeper_identity["model"],
            family=bookkeeper_identity["family"],
            evidence_hash=evidence.content_hash,
            context_hash=context_fingerprint(bookkeeper_context),
            result={
                "decision": "approve",
                "confidence": proposal["confidence"],
                "rationale": proposal["rationale"],
            },
            response_hash=proposal_response_hash,
        )
        controller_context = build_agent_context(
            agent_role="controller",
            entity=_entity_context(entity),
            evidence_hash=evidence.content_hash,
            extracted_facts=facts,
            chart=chart,
            candidate=_candidate_context(candidate),
        )
        controller, controller_response_hash, controller_identity = call_agent(
            config,
            role="controller",
            model=config.controller_model,
            context=controller_context,
            client=client,
        )
        _record_decision(
            control_db,
            run=run,
            candidate=candidate,
            role="controller",
            model=controller_identity["model"],
            family=controller_identity["family"],
            evidence_hash=evidence.content_hash,
            context_hash=context_fingerprint(controller_context),
            result=controller,
            response_hash=controller_response_hash,
        )
        control_db.flush()
        gate = evaluate_candidate(
            control_db,
            ledger_db,
            candidate,
            autonomous=entity.posting_mode == "autopost_ordinary",
        )
        candidate.deterministic_checks = gate
        candidate.status = "ready" if gate["passed"] else "blocked"
        candidate.block_reason = None if gate["passed"] else ", ".join(gate["failed"])
        run.status = "completed"
        run.summary = {
            "outcome": "candidate_created",
            "candidate_id": candidate.id,
            "bookkeeper_confidence": float(proposal["confidence"]),
            "controller_decision": controller["decision"],
            "controller_confidence": float(controller["confidence"]),
            "gate_passed": gate["passed"],
            "failed_checks": gate["failed"],
            "autonomous": gate["autonomous"],
        }
        run.completed_at = _now()
        control_db.commit()
        return {"run_id": run.id, "status": run.status, **run.summary}
    except ValueError as exc:
        control_db.rollback()
        run = control_db.get(ArkControllerRun, run.id)
        return _block_run(
            control_db,
            run,
            evidence.entity_id,
            "bookkeeping_contract_failed",
            str(exc),
        )
    except Exception:
        control_db.rollback()
        run = control_db.get(ArkControllerRun, run.id)
        return _block_run(
            control_db,
            run,
            evidence.entity_id,
            "bookkeeping_internal_error",
            "Bookkeeping failed without persisting a posting candidate",
        )


def post_candidate_if_eligible(
    control_db: Session,
    ledger_db: Session,
    candidate: ArkPostingCandidate,
) -> dict:
    """Post only a routine ordinary entry that passes the autonomous gate."""
    entity = control_db.get(ArkEntity, candidate.entity_id)
    if entity is None or entity.posting_mode != "autopost_ordinary":
        return {"status": "not_posted", "reason": "autonomous_posting_disabled"}
    gate = evaluate_candidate(control_db, ledger_db, candidate, autonomous=True)
    candidate.deterministic_checks = gate
    if not gate["passed"]:
        candidate.status = "blocked"
        candidate.block_reason = ", ".join(gate["failed"])
        control_db.commit()
        return {"status": "not_posted", "reason": candidate.block_reason, "gate": gate}
    if ledger_db.bind.dialect.name == "postgresql":
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
    if existing:
        candidate.status = "posted"
        candidate.posted_transaction_id = existing.id
        control_db.commit()
        return {"status": "posted", "transaction_id": existing.id, "idempotent": True}
    candidate.status = "posting"
    candidate.block_reason = None
    control_db.commit()
    try:
        transaction = create_journal_entry(
            ledger_db,
            candidate.transaction_date,
            candidate.description,
            candidate.lines,
            source_type=ARKCPA_LEDGER_SOURCE_TYPE,
            source_id=candidate.evidence_id,
            reference=candidate.reference,
        )
        ledger_db.commit()
        ledger_db.refresh(transaction)
    except Exception:
        ledger_db.rollback()
        candidate.status = "blocked"
        candidate.block_reason = "ledger posting failed; no entry committed"
        control_db.commit()
        raise
    candidate.status = "posted"
    candidate.posted_transaction_id = transaction.id
    control_db.commit()
    return {"status": "posted", "transaction_id": transaction.id, "idempotent": False}
