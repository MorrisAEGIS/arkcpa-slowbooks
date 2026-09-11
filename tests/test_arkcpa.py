from datetime import date
from decimal import Decimal

import pytest

from app.database import entity_database_url
from app.models.arkcpa import (
    ArkAgentDecision,
    ArkEntity,
    ArkEntityAccess,
    ArkEvidence,
    ArkPostingCandidate,
)
from app.models.users import User
from app.services import ark_files
from app.services.ark_files import scan_entity_files
from app.services.arkcpa_agents import build_agent_context, context_fingerprint
from app.services.arkcpa_policy import evaluate_candidate
from app.services.arkcpa_rules import POLICY_VERSION


def _control_fixture(db, accounts):
    user = db.query(User).filter(User.username == "admin").one()
    entity = ArkEntity(
        name="Test Alberta Corporation",
        slug="test-ab-corp",
        entity_type="ca_ab_corporation",
        jurisdiction="Canada / Alberta",
        status="active",
        database_name="arkcpa_test_ab_corp",
        ark_files_path="_shared/Ark CPA/test-ab-corp",
        currency="CAD",
        fiscal_year_end_month=12,
        fiscal_year_end_day=31,
        payroll_enabled=False,
        posting_mode="assisted",
        facts_status="verified",
        profile={},
    )
    db.add(entity)
    db.flush()
    db.add(
        ArkEntityAccess(
            entity_id=entity.id, user_id=user.id, role="admin", is_default=True
        )
    )
    evidence = ArkEvidence(
        entity_id=entity.id,
        source_path="_shared/Ark CPA/test-ab-corp/Receipts/receipt.pdf",
        category="Receipts",
        content_hash="a" * 64,
        size_bytes=100,
        mime_type="application/pdf",
        status="verified",
    )
    db.add(evidence)
    db.flush()
    candidate = ArkPostingCandidate(
        entity_id=entity.id,
        evidence_id=evidence.id,
        transaction_date=date(2026, 9, 1),
        description="Office supplies",
        currency="CAD",
        lines=[
            {"account_id": accounts["6000"].id, "debit": "125.00", "credit": "0"},
            {"account_id": accounts["1000"].id, "debit": "0", "credit": "125.00"},
        ],
        tax_context={"tax_code": "gst_itc"},
        action_type="ordinary_entry",
        status="reviewing",
        deterministic_checks={},
        created_by="admin",
    )
    db.add(candidate)
    db.flush()
    return entity, evidence, candidate


def _decision(candidate, role, family, confidence="0.9900"):
    return ArkAgentDecision(
        candidate_id=candidate.id,
        agent_role=role,
        model_id=f"ark-{role}-test",
        model_family=family,
        prompt_version="test-v1",
        policy_version=POLICY_VERSION,
        confidence=Decimal(confidence),
        decision="approve",
        evidence_hash="a" * 64,
        rationale_hash=("b" if role == "bookkeeper" else "c") * 64,
    )


def test_independent_high_confidence_pair_passes_posting_gate(
    client, db_session, seed_accounts
):
    _entity, _evidence, candidate = _control_fixture(db_session, seed_accounts)
    db_session.add(_decision(candidate, "bookkeeper", "family-a"))
    db_session.add(_decision(candidate, "controller", "family-b"))
    db_session.commit()

    result = evaluate_candidate(db_session, db_session, candidate)

    assert result["passed"] is True
    assert result["failed"] == []
    assert result["totals"] == {"debits": "125.00", "credits": "125.00"}


@pytest.mark.parametrize(
    ("controller_family", "controller_confidence", "failed_check"),
    [
        ("family-a", "0.9900", "independent_model_families"),
        ("family-b", "0.9799", "controller_approved"),
    ],
)
def test_posting_gate_fails_closed_on_agent_independence_or_confidence(
    client,
    db_session,
    seed_accounts,
    controller_family,
    controller_confidence,
    failed_check,
):
    _entity, _evidence, candidate = _control_fixture(db_session, seed_accounts)
    db_session.add(_decision(candidate, "bookkeeper", "family-a"))
    db_session.add(
        _decision(candidate, "controller", controller_family, controller_confidence)
    )
    db_session.commit()

    result = evaluate_candidate(db_session, db_session, candidate)

    assert result["passed"] is False
    assert failed_check in result["failed"]


def test_draft_only_mode_blocks_ledger_posting(client, db_session, seed_accounts):
    entity, _evidence, candidate = _control_fixture(db_session, seed_accounts)
    entity.posting_mode = "draft_only"
    db_session.add(_decision(candidate, "bookkeeper", "family-a"))
    db_session.add(_decision(candidate, "controller", "family-b"))
    db_session.commit()

    result = evaluate_candidate(db_session, db_session, candidate)

    assert result["passed"] is False
    assert "posting_mode_allows_entry" in result["failed"]


def test_agent_approvals_are_invalidated_when_evidence_hash_changes(
    client, db_session, seed_accounts
):
    _entity, evidence, candidate = _control_fixture(db_session, seed_accounts)
    db_session.add(_decision(candidate, "bookkeeper", "family-a"))
    db_session.add(_decision(candidate, "controller", "family-b"))
    db_session.commit()
    evidence.content_hash = "f" * 64
    db_session.commit()

    result = evaluate_candidate(db_session, db_session, candidate)

    assert result["passed"] is False
    assert "decisions_match_evidence" in result["failed"]


def test_protected_filing_is_recorded_but_never_ready_to_post(
    client, db_session, seed_accounts
):
    entity, evidence, _candidate = _control_fixture(db_session, seed_accounts)
    db_session.commit()
    response = client.post(
        f"/api/ark-cpa/entities/{entity.id}/candidates",
        json={
            "evidence_id": evidence.id,
            "transaction_date": "2026-09-01",
            "description": "Submit T2",
            "currency": "CAD",
            "lines": [
                {"account_id": seed_accounts["6000"].id, "debit": 1, "credit": 0},
                {"account_id": seed_accounts["1000"].id, "debit": 0, "credit": 1},
            ],
            "tax_context": {"tax_code": "none"},
            "action_type": "filing",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "blocked"
    post = client.post(f"/api/ark-cpa/candidates/{response.json()['id']}/post")
    assert post.status_code == 409
    assert "ordinary_entry" in post.json()["detail"]["failed"]
    protected = client.get("/api/ark-cpa/protected-actions").json()
    assert any(
        row["action_type"] == "filing" and row["status"] == "required"
        for row in protected
    )


def test_human_cannot_forge_an_agent_decision(client, db_session, seed_accounts):
    _entity, _evidence, candidate = _control_fixture(db_session, seed_accounts)
    db_session.commit()
    response = client.put(
        f"/api/ark-cpa/candidates/{candidate.id}/decision",
        json={
            "agent_role": "controller",
            "model_id": "human-typed",
            "model_family": "none",
            "prompt_version": "none",
            "confidence": 1,
            "decision": "approve",
            "rationale": "trust me",
        },
    )
    assert response.status_code == 403


def test_ark_files_scan_hashes_allowed_files_and_quarantines_unsupported(
    client, db_session, seed_accounts, tmp_path, monkeypatch
):
    entity, _evidence, _candidate = _control_fixture(db_session, seed_accounts)
    root = tmp_path / "_shared" / "Ark CPA" / entity.slug
    receipts = root / "Receipts"
    receipts.mkdir(parents=True)
    (receipts / "receipt.pdf").write_bytes(b"%PDF-1.4 safe fixture")
    (receipts / "payload.exe").write_bytes(b"not allowed")
    monkeypatch.setattr(ark_files, "ARK_FILES_ROOT", tmp_path.resolve())

    result = scan_entity_files(db_session, entity)

    assert result["indexed"] == 1
    assert result["quarantined"] == 1
    safe = (
        db_session.query(ArkEvidence)
        .filter(ArkEvidence.source_path.like("%receipt.pdf"))
        .one()
    )
    blocked = (
        db_session.query(ArkEvidence)
        .filter(ArkEvidence.source_path.like("%payload.exe"))
        .one()
    )
    assert len(safe.content_hash) == 64
    assert blocked.status == "quarantined"
    assert blocked.content_hash is None


def test_entity_database_url_rejects_identifier_injection(monkeypatch):
    import app.database as database

    monkeypatch.setattr(
        database, "DATABASE_URL", "postgresql://user:pass@db:5432/arkcpa"
    )
    assert entity_database_url("arkcpa_personal").endswith("/arkcpa_personal")
    with pytest.raises(ValueError):
        entity_database_url("arkcpa;drop database postgres")


def test_arkcpa_status_exposes_safety_contract(client):
    status = client.get("/api/ark-cpa/status")
    assert status.status_code == 200
    body = status.json()
    assert body["product"] == "Ark CPA"
    assert body["autonomous_confidence_threshold"] == 0.98
    assert body["raw_documents_in_model_memory"] is False
    assert body["payroll"] == "evidence-and-reminders-only"


def test_entity_rejects_impossible_fiscal_year_end(client):
    response = client.post(
        "/api/ark-cpa/entities",
        json={
            "name": "Impossible Year End Corp",
            "slug": "impossible-year-end",
            "entity_type": "ca_ab_corporation",
            "database_name": "impossible_year_end",
            "jurisdiction": "Canada / Alberta",
            "currency": "CAD",
            "fiscal_year_end_month": 2,
            "fiscal_year_end_day": 31,
            "status": "active",
        },
    )
    assert response.status_code == 422
    assert "Fiscal year end" in response.text


def test_agent_context_redacts_identifiers_and_refuses_raw_document_text():
    context = build_agent_context(
        agent_role="bookkeeper",
        entity={"name": "Test", "entity_type": "ca_ab_corporation"},
        evidence_hash="d" * 64,
        extracted_facts={"total": "100.00", "tax_id": "123", "email": "x@example.com"},
        chart=[{"account_id": 1, "name": "Cash"}],
    )
    assert context["evidence"]["facts"]["tax_id"] == "[REDACTED]"
    assert context["evidence"]["facts"]["email"] == "[REDACTED]"
    assert len(context_fingerprint(context)) == 64

    with pytest.raises(ValueError, match="forbidden"):
        build_agent_context(
            agent_role="bookkeeper",
            entity={"name": "Test"},
            evidence_hash="d" * 64,
            extracted_facts={"raw_text": "entire receipt"},
            chart=[],
        )


def test_controller_context_requires_bookkeeper_candidate():
    with pytest.raises(ValueError, match="Bookkeeper candidate"):
        build_agent_context(
            agent_role="controller",
            entity={"name": "Test"},
            evidence_hash="e" * 64,
            extracted_facts={"total": "10.00"},
            chart=[],
        )
