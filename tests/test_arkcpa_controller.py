"""Ark CPA Controller, protected-owner, and autonomous-pattern gates."""

from datetime import date
from decimal import Decimal
import json

import httpx
import pytest

from app.models.arkcpa import (
    ArkAgentDecision,
    ArkAgentDecisionRun,
    ArkAgentGrant,
    ArkControllerRun,
    ArkEntity,
    ArkEntityAccess,
    ArkEvidence,
    ArkEvidenceFact,
    ArkPostingCandidate,
    ArkProtectedAction,
)
from app.models.transactions import Transaction
from app.models.users import User
from app.routes.arkcpa import EntityGovernanceUpdate
from app.services.arkcpa_controller import (
    ControllerConfig,
    _entity_context,
    call_agent,
    call_bookkeeper_proposal,
    post_candidate_if_eligible,
    run_evidence_bookkeeping,
)
from app.services.arkcpa_agents import PROMPT_VERSION, system_prompt
from app.services.arkcpa_policy import evaluate_candidate
from app.services.arkcpa_rules import POLICY_VERSION
from app.services.auth import hash_password


def _seed_entity(db, accounts, *, posting_mode="assisted"):
    user = db.query(User).filter_by(username="admin").one()
    entity = ArkEntity(
        name="Stage Alberta Corporation",
        slug="stage-controller-corp",
        entity_type="ca_ab_corporation",
        jurisdiction="Canada / Alberta",
        status="active",
        database_name="stage_controller_corp",
        ark_files_path="_shared/Ark CPA/stage-controller-corp",
        currency="CAD",
        fiscal_year_end_month=12,
        fiscal_year_end_day=31,
        payroll_enabled=False,
        posting_mode=posting_mode,
        facts_status="verified",
        profile={
            "business_activity": "energy services",
            "revenue_status": "pre-revenue",
        },
    )
    db.add(entity)
    db.flush()
    access = ArkEntityAccess(
        entity_id=entity.id,
        user_id=user.id,
        role="admin",
        is_default=True,
        protected_approver=True,
    )
    db.add(access)
    evidence = ArkEvidence(
        entity_id=entity.id,
        source_path="_shared/Ark CPA/stage-controller-corp/Receipts/test.txt",
        category="Receipts",
        content_hash="a" * 64,
        size_bytes=80,
        mime_type="text/plain",
        status="verified",
        extracted_text_hash="b" * 64,
        extractor_version="test",
    )
    db.add(evidence)
    db.flush()
    db.add(
        ArkEvidenceFact(
            evidence_id=evidence.id,
            fact_key="receipt.total",
            value={"value": "125.00"},
            confidence=Decimal("0.9900"),
            locator="document",
            source_hash=evidence.content_hash,
            extractor_version="test",
            status="verified",
        )
    )
    db.commit()
    return entity, access, evidence


def _config(**changes):
    values = {
        "base_url": "http://localhost:4000/v1",
        "api_key": "test-only",
        "bookkeeper_model": "ark-bookkeeper-test",
        "bookkeeper_family": "family-a",
        "controller_model": "ark-controller-test",
        "controller_family": "family-b",
    }
    values.update(changes)
    return ControllerConfig(**values)


def test_verified_evidence_produces_append_only_independent_agent_receipts(
    client, db_session, seed_accounts
):
    entity, _access, evidence = _seed_entity(db_session, seed_accounts)
    responses = iter(
        [
            {
                "no_candidate": False,
                "candidate": {
                    "transaction_date": "2026-09-01",
                    "description": "Office supplies",
                    "reference": "R-100",
                    "currency": "CAD",
                    "lines": [
                        {
                            "account_id": seed_accounts["6000"].id,
                            "debit": "125.00",
                            "credit": "0",
                            "description": "Supplies",
                        },
                        {
                            "account_id": seed_accounts["1000"].id,
                            "debit": "0",
                            "credit": "125.00",
                            "description": "Cash",
                        },
                    ],
                    "tax_context": {"tax_code": "gst_itc"},
                },
                "confidence": 0.99,
                "rationale": "Verified total and active chart mapping agree.",
            },
            {
                "decision": "approve",
                "confidence": 0.995,
                "rationale": "Independent re-performance passed.",
                "checks": {"balanced": True, "evidence": True},
            },
        ]
    )
    identities = iter(
        (("resolved-bookkeeper", "family-a"), ("resolved-controller", "family-b"))
    )

    def handler(_request):
        body = next(responses)
        model, family = next(identities)
        return httpx.Response(
            200,
            json={
                "model": model,
                "model_family": family,
                "choices": [{"message": {"content": json.dumps(body)}}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as model_client:
        result = run_evidence_bookkeeping(
            db_session,
            db_session,
            evidence,
            config=_config(),
            client=model_client,
        )

    assert result["status"] == "completed"
    assert result["outcome"] == "candidate_created"
    assert result["gate_passed"] is True
    candidate = db_session.get(ArkPostingCandidate, result["candidate_id"])
    assert candidate.entity_id == entity.id
    assert candidate.status == "ready"
    receipts = (
        db_session.query(ArkAgentDecisionRun)
        .filter_by(candidate_id=candidate.id)
        .order_by(ArkAgentDecisionRun.id)
        .all()
    )
    assert [row.agent_role for row in receipts] == ["bookkeeper", "controller"]
    assert len({row.model_family for row in receipts}) == 2
    assert all(len(row.context_hash) == 64 for row in receipts)
    assert db_session.query(ArkControllerRun).filter_by(status="completed").count() == 1


def test_controller_configuration_rejects_same_model_family(monkeypatch):
    monkeypatch.setenv("ARKCPA_AI_ALLOWED_HOSTS", "localhost")
    with pytest.raises(ValueError, match="independent model families"):
        _config(controller_family="family-a").validate()


def test_agent_entity_context_omits_legal_name_and_non_allowlisted_profile(db_session):
    entity = ArkEntity(
        id=17,
        name="Exact Private Legal Name",
        slug="private-entity",
        entity_type="ca_ab_corporation",
        jurisdiction="Canada / Alberta",
        status="active",
        database_name="private_entity",
        ark_files_path="_shared/Ark CPA/private-entity",
        currency="CAD",
        fiscal_year_end_month=12,
        fiscal_year_end_day=31,
        posting_mode="draft_only",
        facts_status="incomplete",
        profile={
            "business_activity": "energy services",
            "owners": ["Private Person"],
        },
    )

    context = _entity_context(entity)

    assert "name" not in context
    assert context["entity_ref"] != entity.slug
    assert context["profile"] == {"business_activity": "energy services"}


def test_agent_call_requires_gateway_model_family_attestation():
    response = {
        "decision": "approve",
        "confidence": 0.99,
        "rationale": "Checks passed.",
        "checks": {},
    }

    def handler(_request):
        return httpx.Response(
            200,
            json={
                "model": "resolved",
                "choices": [{"message": {"content": json.dumps(response)}}],
            },
        )

    context = {"contract": {"raw_documents_allowed": False}}
    with httpx.Client(transport=httpx.MockTransport(handler)) as model_client:
        with pytest.raises(ValueError, match="attest the expected model family"):
            call_agent(
                _config(),
                role="controller",
                model="ark-controller-test",
                context=context,
                client=model_client,
            )


def test_governed_prompt_names_every_required_decision_key():
    prompt = system_prompt("controller")

    assert PROMPT_VERSION == "arkcpa-agents-2026-09-12.2"
    for key in ("decision", "confidence", "rationale", "checks"):
        assert f'"{key}"' in prompt
    assert "Do not use a key named" in prompt
    assert '"fields"' in prompt


def test_gpt_oss_bookkeeper_requests_use_strict_json_without_reasoning():
    requests = []
    decision = {
        "decision": "reject",
        "confidence": 1,
        "rationale": "No verified evidence.",
        "checks": {},
    }
    proposal = {
        "no_candidate": True,
        "candidate": None,
        "confidence": 1,
        "rationale": "No verified evidence.",
    }
    responses = iter((decision, proposal))

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "ark-coder-oss",
                "model_family": "gpt-oss",
                "choices": [{"message": {"content": json.dumps(next(responses))}}],
            },
        )

    config = _config(
        bookkeeper_model="ark-coder-oss",
        bookkeeper_family="gpt-oss",
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as model_client:
        call_agent(
            config,
            role="bookkeeper",
            model=config.bookkeeper_model,
            context={"contract": {"raw_documents_allowed": False}},
            client=model_client,
        )
        call_bookkeeper_proposal(
            config,
            context={"contract": {"raw_documents_allowed": False}},
            client=model_client,
        )

    assert [request["thinking_budget_tokens"] for request in requests] == [0, 0]
    assert [request["reasoning_format"] for request in requests] == ["none", "none"]
    assert [request["response_format"] for request in requests] == [
        {"type": "json_object"},
        {"type": "json_object"},
    ]


def test_controller_default_uses_independent_max_context_route(monkeypatch):
    monkeypatch.setenv("ARKCPA_AI_BASE_URL", "http://localhost:4000/v1")
    monkeypatch.delenv("ARKCPA_CONTROLLER_MODEL", raising=False)

    assert ControllerConfig.from_env().controller_model == "ark-brain-max"


def _approval(candidate, role, family):
    return ArkAgentDecision(
        candidate_id=candidate.id,
        agent_role=role,
        model_id=f"{role}-model",
        model_family=family,
        prompt_version="test",
        policy_version=POLICY_VERSION,
        confidence=Decimal("0.9900"),
        decision="approve",
        evidence_hash="a" * 64,
        rationale_hash=("c" if role == "controller" else "b") * 64,
    )


def test_autonomous_posting_requires_learned_routine_pattern(
    client, db_session, seed_accounts
):
    entity, _access, evidence = _seed_entity(
        db_session, seed_accounts, posting_mode="autopost_ordinary"
    )
    lines = [
        {"account_id": seed_accounts["6000"].id, "debit": "105.00", "credit": "0"},
        {"account_id": seed_accounts["1000"].id, "debit": "0", "credit": "105.00"},
    ]
    for index, amount in enumerate(("90.00", "100.00", "110.00"), start=1):
        prior_lines = [dict(line) for line in lines]
        prior_lines[0]["debit"] = amount
        prior_lines[1]["credit"] = amount
        db_session.add(
            ArkPostingCandidate(
                entity_id=entity.id,
                evidence_id=evidence.id,
                transaction_date=date(2026, 8, index),
                description="Office supplies",
                currency="CAD",
                lines=prior_lines,
                tax_context={"tax_code": "gst_itc"},
                action_type="ordinary_entry",
                status="posted",
                deterministic_checks={},
                created_by="test",
            )
        )
    candidate = ArkPostingCandidate(
        entity_id=entity.id,
        evidence_id=evidence.id,
        transaction_date=date(2026, 9, 1),
        description="Office supplies",
        currency="CAD",
        lines=lines,
        tax_context={"tax_code": "gst_itc"},
        action_type="ordinary_entry",
        status="reviewing",
        deterministic_checks={},
        created_by="ark-bookkeeper",
    )
    db_session.add(candidate)
    db_session.flush()
    db_session.add_all(
        [
            _approval(candidate, "bookkeeper", "family-a"),
            _approval(candidate, "controller", "family-b"),
        ]
    )
    db_session.commit()

    result = evaluate_candidate(db_session, db_session, candidate, autonomous=True)

    assert result["passed"] is True
    assert result["checks"]["routine_pattern"] is True

    candidate.lines[0]["debit"] = "500.00"
    candidate.lines[1]["credit"] = "500.00"
    result = evaluate_candidate(db_session, db_session, candidate, autonomous=True)
    assert result["passed"] is False
    assert "routine_pattern" in result["failed"]


def test_autonomous_posting_is_idempotent_per_evidence(
    client, db_session, seed_accounts
):
    entity, _access, evidence = _seed_entity(
        db_session, seed_accounts, posting_mode="autopost_ordinary"
    )
    lines = [
        {"account_id": seed_accounts["6000"].id, "debit": "100.00", "credit": "0"},
        {"account_id": seed_accounts["1000"].id, "debit": "0", "credit": "100.00"},
    ]
    for day in (1, 2, 3):
        db_session.add(
            ArkPostingCandidate(
                entity_id=entity.id,
                evidence_id=evidence.id,
                transaction_date=date(2026, 8, day),
                description="Office supplies",
                currency="CAD",
                lines=lines,
                tax_context={"tax_code": "gst_itc"},
                action_type="ordinary_entry",
                status="posted",
                deterministic_checks={},
                created_by="test",
            )
        )
    db_session.flush()

    def new_candidate():
        row = ArkPostingCandidate(
            entity_id=entity.id,
            evidence_id=evidence.id,
            transaction_date=date(2026, 9, 1),
            description="Office supplies",
            currency="CAD",
            lines=lines,
            tax_context={"tax_code": "gst_itc"},
            action_type="ordinary_entry",
            status="ready",
            deterministic_checks={},
            created_by="ark-bookkeeper",
        )
        db_session.add(row)
        db_session.flush()
        db_session.add_all(
            [
                _approval(row, "bookkeeper", "family-a"),
                _approval(row, "controller", "family-b"),
            ]
        )
        db_session.commit()
        return row

    first = post_candidate_if_eligible(db_session, db_session, new_candidate())
    second = post_candidate_if_eligible(db_session, db_session, new_candidate())

    assert first["status"] == "posted"
    assert first["idempotent"] is False
    assert second == {
        "status": "posted",
        "transaction_id": first["transaction_id"],
        "idempotent": True,
    }
    transactions = db_session.query(Transaction).filter_by(
        source_type="arkcpa_evidence", source_id=evidence.id
    )
    assert transactions.count() == 1


def test_only_protected_owner_can_record_protected_decision(
    client, db_session, seed_accounts
):
    entity, access, _evidence = _seed_entity(db_session, seed_accounts)
    action = ArkProtectedAction(
        entity_id=entity.id,
        action_type="filing",
        reason="Submit a return",
        requested_by="controller",
    )
    db_session.add(action)
    db_session.commit()

    approved = client.put(
        f"/api/ark-cpa/protected-actions/{action.id}/decision",
        json={"decision": "approved", "note": "Reviewed for external filing."},
    )
    assert approved.status_code == 200
    assert approved.json()["execution_performed"] is False

    second = ArkProtectedAction(
        entity_id=entity.id,
        action_type="money_movement",
        reason="Pay balance",
        requested_by="controller",
    )
    db_session.add(second)
    access.protected_approver = False
    db_session.commit()
    refused = client.put(
        f"/api/ark-cpa/protected-actions/{second.id}/decision",
        json={"decision": "approved", "note": "Attempt without authority."},
    )
    assert refused.status_code == 403


def test_entity_admin_scope_does_not_grant_protected_or_global_admin(
    client, db_session, seed_accounts
):
    entity, _access, _evidence = _seed_entity(db_session, seed_accounts)
    maesa = User(
        username="maesa",
        display_name="Maesa",
        password_hash=hash_password("maesa-test-password"),
        role="bookkeeper",
        is_active=True,
    )
    db_session.add(maesa)
    db_session.flush()
    db_session.add(
        ArkEntityAccess(
            entity_id=entity.id,
            user_id=maesa.id,
            role="admin",
            protected_approver=False,
        )
    )
    db_session.commit()
    client.post("/api/auth/logout")
    login = client.post(
        "/api/auth/login",
        json={"username": "maesa", "password": "maesa-test-password"},
    )
    assert login.status_code == 200

    entity_admin = client.put(
        f"/api/ark-cpa/entities/{entity.id}/access",
        json={
            "username": "maesa",
            "role": "admin",
            "is_default": True,
            "protected_approver": False,
        },
    )
    assert entity_admin.status_code == 200
    assert client.get("/api/ark-cpa/authority-sources").status_code == 200
    assert (
        client.put(
            f"/api/ark-cpa/entities/{entity.id}/governance",
            json={
                "posting_mode": "assisted",
                "facts_status": "verified",
                "profile": {},
            },
        ).status_code
        == 403
    )
    assert client.get("/api/users").status_code == 403


def test_agent_token_cannot_cross_entity_boundary(client, db_session, seed_accounts):
    entity, _access, _evidence = _seed_entity(db_session, seed_accounts)
    other = ArkEntity(
        name="Other Ledger",
        slug="other-ledger",
        entity_type="ca_ab_corporation",
        jurisdiction="Canada / Alberta",
        status="active",
        database_name="other_ledger",
        ark_files_path="_shared/Ark CPA/other-ledger",
        currency="CAD",
        fiscal_year_end_month=12,
        fiscal_year_end_day=31,
        posting_mode="draft_only",
        facts_status="incomplete",
        profile={},
    )
    db_session.add(other)
    db_session.commit()
    created = client.post(
        "/api/tokens", json={"label": "entity-bookkeeper", "role": "bookkeeper"}
    )
    assert created.status_code == 201
    token_id = created.json()["id"]
    db_session.add(
        ArkAgentGrant(
            entity_id=entity.id,
            token_id=token_id,
            agent_role="bookkeeper",
            model_id="ark-bookkeeper-test",
            model_family="family-a",
            prompt_version="test-v1",
            is_active=True,
        )
    )
    db_session.commit()
    headers = {"Authorization": f"Bearer {created.json()['token']}"}
    assert client.post("/api/auth/logout").status_code == 200

    assert (
        client.get(
            f"/api/ark-cpa/entities/{entity.id}/candidates", headers=headers
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/ark-cpa/entities/{other.id}/candidates", headers=headers
        ).status_code
        == 403
    )
    visible = client.get("/api/ark-cpa/entities", headers=headers).json()
    assert [row["id"] for row in visible] == [entity.id]


def test_entity_profile_rejects_sensitive_identifiers_at_api_boundary():
    with pytest.raises(ValueError, match="Sensitive entity profile"):
        EntityGovernanceUpdate(
            posting_mode="assisted",
            facts_status="verified",
            profile={"ein": "never-store-this"},
        )
