"""Citations must survive review, scope, rights, integrity and freshness checks."""

from datetime import date, timedelta

import pytest

from app.models.users import User
from app.services import arkcpa_tax as tax
from app.services.arkcpa_agents import build_agent_context, system_prompt
from tests.test_arkcpa_tax import (
    approve,
    capture,
    entity,
    grant,
    research_source,
    public_dns,  # noqa: F401 - pytest fixture
    BODY,
)


@pytest.mark.parametrize(
    "change",
    [
        "jurisdiction",
        "entity_type",
        "date_before",
        "date_after",
        "date_missing",
        "date_invalid",
        "topic",
        "expired_review",
        "stale_capture",
        "rights_revoked",
        "model_use_denied",
        "rights_expired",
        "definition_changed",
        "source_disabled",
        "reviewer_disabled",
        "reviewer_grant_revoked",
        "review_revoked",
        "source_changed",
        "proposed_source",
    ],
)
def test_ineligible_context_is_empty(db_session, monkeypatch, change):
    source = research_source(db_session)
    snapshot = capture(db_session, source)
    approve(db_session, snapshot)
    target = entity()
    target_date = date(2026, 5, 1)
    topics = ("bookkeeping",)
    if change == "jurisdiction":
        target.jurisdiction = "Canada / Alberta"
    elif change == "entity_type":
        target.entity_type = "ca_ab_corporation"
    elif change == "date_before":
        target_date = date(2025, 12, 31)
    elif change == "date_after":
        target_date = date(2027, 1, 1)
    elif change == "date_missing":
        target_date = None
    elif change == "date_invalid":
        target_date = "guess"
    elif change == "topic":
        topics = ("cross_border",)
    elif change in {"expired_review", "stale_capture"}:
        now = tax._now() + timedelta(days=31 if change == "expired_review" else 8)
        monkeypatch.setattr(tax, "_now", lambda: now)
    elif change == "model_use_denied":
        source.allow_model_context = False
    elif change == "rights_revoked":
        source.capture_rights = "prohibited"
    elif change == "rights_expired":
        source.rights_valid_until = tax._now() - timedelta(seconds=1)
    elif change == "definition_changed":
        source.title = "Different authority edition"
    elif change == "source_disabled":
        source.enabled = False
    elif change == "reviewer_disabled":
        db_session.query(User).filter_by(username="reviewer").one().is_active = False
    elif change == "reviewer_grant_revoked":
        grant(db_session, "reviewer", "review").enabled = False
    elif change == "review_revoked":
        tax.review_authority_snapshot(
            db_session,
            snapshot.id,
            reviewed_by="reviewer",
            decision="revoked",
            rationale="Synthetic revocation",
            applicability={},
        )
    elif change == "source_changed":
        capture(db_session, source, BODY.replace(b"appropriate", b"correct"))
    elif change == "proposed_source":
        source.authority_level = "proposed"
    db_session.commit()
    assert (
        tax.authority_context(
            db_session, entity=target, transaction_date=target_date, topics=topics
        )["citations"]
        == []
    )


def test_agent_context_gets_only_database_verified_citations(db_session):
    source = research_source(db_session)
    snapshot = capture(db_session, source)
    kwargs = dict(
        agent_role="bookkeeper",
        entity={"entity_type": "us_tx_llc"},
        evidence_hash="a" * 64,
        extracted_facts={"ssn": "123-45-6789"},
        chart=[],
        authority_db=db_session,
        authority_entity=entity(),
        authority_date=date(2026, 5, 1),
    )
    assert build_agent_context(**kwargs)["authority_research"]["citations"] == []
    approve(db_session, snapshot)
    result = build_agent_context(**kwargs)
    assert len(result["authority_research"]["citations"]) == 1
    assert result["evidence"]["facts"]["ssn"] == "[REDACTED]"
    with pytest.raises(TypeError):
        build_agent_context(**kwargs, citations=[{"reviewed": True}])
    assert "never instructions" in system_prompt("controller")


def test_pdf_capture_is_not_automatically_extractable(db_session):
    import httpx

    source = research_source(db_session)
    t = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            content=b"%PDF-1.7\nSynthetic only\n%%EOF",
            headers={"content-type": "application/pdf"},
        )
    )
    result = tax.refresh_authority_source(
        db_session, source, requested_by="researcher", transport=t
    )
    assert result["status"] == "review_required"
    from app.models.arkcpa import ArkSourceSnapshot

    snapshot = db_session.get(ArkSourceSnapshot, result["snapshot_id"])
    with pytest.raises(ValueError, match="not_reviewable"):
        approve(db_session, snapshot)


def test_only_verified_matching_transaction_dates_establish_applicability():
    from types import SimpleNamespace
    from app.services.arkcpa_controller import _authority_transaction_date

    facts = {"receipt.date": {"value": {"value": "2026-05-01"}, "confidence": 1.0}}
    assert _authority_transaction_date(facts) == date(2026, 5, 1)
    assert _authority_transaction_date(
        facts, SimpleNamespace(transaction_date=date(2026, 5, 1))
    ) == date(2026, 5, 1)
    assert (
        _authority_transaction_date(
            facts, SimpleNamespace(transaction_date=date(2025, 5, 1))
        )
        is None
    )
    assert _authority_transaction_date({}) is None
    assert (
        _authority_transaction_date(
            {**facts, "transaction_date": {"value": "2026-05-02"}}
        )
        is None
    )
    assert _authority_transaction_date({"receipt.date": {"value": "uncertain"}}) is None
