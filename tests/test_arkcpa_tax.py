"""Official tax-source changes stay proposals until protected promotion."""

import httpx

from app.models.arkcpa import ArkAuthoritySource, ArkRuleProposal, ArkSourceSnapshot
from app.services.arkcpa_tax import refresh_authority_source, seed_authority_sources


def test_authority_change_creates_unpromoted_rule_proposal(db_session):
    seed_authority_sources(db_session)
    source = db_session.query(ArkAuthoritySource).filter_by(code="CA_INCOME_TAX_ACT").one()
    bodies = iter((b"official baseline", b"official amended text"))

    def handler(_request):
        return httpx.Response(
            200,
            content=next(bodies),
            headers={"content-type": "text/html", "etag": "test"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        baseline = refresh_authority_source(db_session, source, client=client)
        changed = refresh_authority_source(db_session, source, client=client)

    assert baseline["status"] == "baseline_created"
    assert changed["status"] == "change_detected"
    proposal = db_session.query(ArkRuleProposal).one()
    assert proposal.status == "draft"
    assert proposal.proposed_change["automatic_policy_change"] is False
    assert db_session.query(ArkSourceSnapshot).filter_by(status="current").count() == 1
    assert db_session.query(ArkSourceSnapshot).filter_by(status="proposed").count() == 1


def test_authority_source_rejects_unapproved_host(db_session):
    source = ArkAuthoritySource(
        code="BAD",
        title="Untrusted",
        jurisdiction="unknown",
        authority_level="lead",
        url="https://example.invalid/tax",
        enabled=True,
    )
    db_session.add(source)
    db_session.commit()

    try:
        refresh_authority_source(db_session, source)
    except ValueError as exc:
        assert "allowlist" in str(exc)
    else:
        raise AssertionError("unapproved authority host was accepted")
