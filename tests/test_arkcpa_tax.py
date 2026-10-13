"""Research infrastructure tests. These are not accounting competency cases."""

from datetime import date, timedelta
from hashlib import sha256
import socket
import gzip
from types import SimpleNamespace

import httpx
import pytest

from app.models.arkcpa import (
    ArkAuthoritySource,
    ArkAuthorityRetrieval,
    ArkAuthorityReview,
    ArkResearchPermission,
    ArkRuleProposal,
    ArkSourceSnapshot,
)
from app.models.users import User
from app.services import arkcpa_tax as tax

BODY = b"<html><body><h1>Official synthetic accounting reference</h1><p>Records must be supported by the verified facts and the appropriate accounting period. This synthetic text establishes no actual legal or accounting treatment.</p></body></html>"
PASSAGE = "Records must be supported by the verified facts and the appropriate accounting period."


def grant(db, username, permission):
    user = db.query(User).filter_by(username=username).first()
    if not user:
        user = User(username=username, password_hash="unused", role="readonly")
        db.add(user)
        db.flush()
    existing = (
        db.query(ArkResearchPermission)
        .filter_by(user_id=user.id, permission=permission)
        .first()
    )
    if existing:
        return existing
    row = ArkResearchPermission(
        user_id=user.id,
        permission=permission,
        granted_by="synthetic-operator",
        expires_at=tax._now() + timedelta(days=30),
    )
    db.add(row)
    db.commit()
    return row


def research_source(db):
    grant(db, "researcher", "refresh")
    grant(db, "reviewer", "review")
    source = ArkAuthoritySource(
        code="SYNTHETIC",
        title="Synthetic reference",
        jurisdiction="United States / Texas",
        authority_level="administrative",
        url="https://www.irs.gov/synthetic-test",
        enabled=True,
        capture_rights="public_domain",
        allow_model_context=True,
        rights_basis="Synthetic fixture only",
        rights_reviewed_by="synthetic-operator",
        rights_valid_until=tax._now() + timedelta(days=365),
    )
    db.add(source)
    db.commit()
    return source


def transport(body=BODY, status=200, **headers):
    return httpx.MockTransport(
        lambda request: httpx.Response(
            status, content=body, headers={"content-type": "text/html", **headers}
        )
    )


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ],
    )


def capture(db, source, body=BODY):
    result = tax.refresh_authority_source(
        db, source, requested_by="researcher", transport=transport(body)
    )
    assert result["status"] in {"review_required", "unchanged"}, result
    return db.get(ArkSourceSnapshot, result["snapshot_id"])


def approve(db, snapshot, **overrides):
    params = dict(
        reviewed_by="reviewer",
        decision="approved",
        rationale="Synthetic reviewer checked scope, effective date and passage.",
        applicability={
            "jurisdictions": ["United States / Texas"],
            "entity_types": ["us_tx_llc"],
            "topics": ["bookkeeping"],
        },
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 12, 31),
        valid_until=tax._now() + timedelta(days=30),
        passages=[{"text": PASSAGE, "locator": "paragraph 1"}],
    )
    params.update(overrides)
    return tax.review_authority_snapshot(db, snapshot.id, **params)


def entity():
    return SimpleNamespace(
        jurisdiction="United States / Texas", entity_type="us_tx_llc"
    )


def test_first_capture_is_quarantined_and_every_attempt_is_preserved(db_session):
    source = research_source(db_session)
    first = capture(db_session, source)
    original = (first.retrieved_at, dict(first.snapshot_metadata), first.content)
    second = capture(db_session, source)
    assert second.id == first.id
    assert (first.retrieved_at, first.snapshot_metadata, first.content) == original
    changed = capture(db_session, source, BODY.replace(b"appropriate", b"correct"))
    assert changed.id != first.id
    assert db_session.query(ArkSourceSnapshot).filter_by(status="current").count() == 0
    assert db_session.query(ArkRuleProposal).count() == 2
    assert all(
        p.status == "draft" and not p.proposed_change["automatic_policy_change"]
        for p in db_session.query(ArkRuleProposal)
    )
    assert [
        e.event
        for e in db_session.query(ArkAuthorityRetrieval).order_by(
            ArkAuthorityRetrieval.id
        )
    ] == ["started", "succeeded"] * 3
    assert (
        tax.authority_context(
            db_session, entity=entity(), transaction_date=date(2026, 5, 1)
        )["citations"]
        == []
    )


def test_permissions_denied_by_default_and_expire(db_session):
    source = research_source(db_session)
    for actor in (None, "unknown", "reviewer"):
        with pytest.raises(PermissionError):
            tax.refresh_authority_source(db_session, source, requested_by=actor)
    row = grant(db_session, "researcher", "refresh")
    row.expires_at = tax._now() - timedelta(seconds=1)
    db_session.commit()
    with pytest.raises(PermissionError):
        tax.refresh_all_authority_sources(db_session, requested_by="researcher")
    assert db_session.query(ArkAuthorityRetrieval).count() == 0


def test_seed_never_overwrites_and_refresh_never_seeds(db_session):
    grant(db_session, "researcher", "refresh")
    assert (
        tax.refresh_all_authority_sources(db_session, requested_by="researcher") == []
    )
    assert tax.seed_authority_sources(db_session) == 7
    source = db_session.query(ArkAuthoritySource).first()
    source.title = "Operator reviewed title"
    source.url = "https://www.irs.gov/retained"
    db_session.commit()
    assert tax.seed_authority_sources(db_session) == 0
    assert source.title == "Operator reviewed title" and source.url.endswith("retained")
    assert source.capture_rights == "unreviewed"


@pytest.mark.parametrize("rights", ["unreviewed", "prohibited", "licensed"])
def test_rights_gate_prevents_network(db_session, monkeypatch, rights):
    source = research_source(db_session)
    source.capture_rights = rights
    source.rights_basis = None
    db_session.commit()
    monkeypatch.setattr(
        tax, "fetch_source", lambda *a, **kw: pytest.fail("Network must not run")
    )
    result = tax.refresh_authority_source(db_session, source, requested_by="researcher")
    assert result["status"] == "failed" and "rights" in result["error"]
    assert db_session.query(ArkAuthorityRetrieval).count() == 2


@pytest.mark.parametrize(
    "url",
    [
        "http://www.irs.gov/a",
        "https://example.invalid/a",
        "https://www.irs.gov:8443/a",
        "https://user:secret@www.irs.gov/a",
        "https://www.irs.gov/#fragment",
        "https://www.irs.gov/\\\\bad",
        "https://www.irs.gov/\nbad",
    ],
)
def test_url_allowlist(url):
    with pytest.raises(ValueError, match="allowlist"):
        tax.fetch_source(url, transport=transport())


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "169.254.169.254",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "2002:7f00:1::",
        "224.0.0.1",
        "239.255.255.250",
        "ff02::1",
        "64:ff9b::7f00:1",
    ],
)
def test_private_and_mixed_dns_answers_rejected(monkeypatch, address):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, 1, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, 1, 6, "", (address, 443)),
        ],
    )
    with pytest.raises(ValueError, match="nonpublic"):
        tax.fetch_source("https://www.irs.gov/test", transport=transport())


def test_pinned_destination_host_and_sni_and_redirect_refusal():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.host == "8.8.8.8"
        assert request.headers["host"] == "www.irs.gov"
        assert request.extensions["sni_hostname"] == "www.irs.gov"
        assert request.headers["accept-encoding"] == "identity"
        assert (
            "cookie" not in request.headers and "authorization" not in request.headers
        )
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secrets"})

    with pytest.raises(ValueError, match="http_302"):
        tax.fetch_source(
            "https://www.irs.gov/test", transport=httpx.MockTransport(handler)
        )
    assert len(seen) == 1


@pytest.mark.parametrize(
    "body,headers,error",
    [
        (b"<html><body><title>Request Access</title>" + BODY, {}, "challenge"),
        (b"<html><body>Verify you are human" + BODY, {}, "challenge"),
        (gzip.compress(BODY), {"content-encoding": "gzip"}, "compression"),
        (BODY, {"content-type": "application/octet-stream"}, "media_type"),
        (BODY, {"content-length": "999999999"}, "size_limit"),
        (b"not html", {}, "invalid_html"),
        (BODY, {"content-type": "application/pdf"}, "invalid_pdf"),
    ],
)
def test_wrong_content_and_challenges(body, headers, error):
    with pytest.raises(ValueError, match=error):
        tax.fetch_source(
            "https://www.irs.gov/test", transport=transport(body, **headers)
        )


def test_stream_is_bounded_without_content_length(monkeypatch):
    monkeypatch.setattr(tax, "MAX_SOURCE_BYTES", 70000)
    consumed = []

    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            for i in range(100):
                consumed.append(i)
                yield b"x" * 65536

    t = httpx.MockTransport(
        lambda req: httpx.Response(
            200, stream=Chunks(), headers={"content-type": "text/plain"}
        )
    )
    with pytest.raises(ValueError, match="size_limit"):
        tax.fetch_source("https://www.irs.gov/test", transport=t)
    assert len(consumed) <= 2


def test_failed_and_interrupted_attempts_ineligible(db_session):
    source = research_source(db_session)
    snapshot = capture(db_session, source)
    approve(db_session, snapshot)
    assert tax.authority_context(
        db_session, entity=entity(), transaction_date=date(2026, 5, 1)
    )["citations"]
    result = tax.refresh_authority_source(
        db_session, source, requested_by="researcher", transport=transport(status=503)
    )
    assert result["status"] == "failed"
    assert (
        tax.authority_context(
            db_session, entity=entity(), transaction_date=date(2026, 5, 1)
        )["citations"]
        == []
    )
    capture(db_session, source)
    db_session.add(
        ArkAuthorityRetrieval(
            attempt_id="synthetic-interrupted",
            source_id=source.id,
            event="started",
            requested_by="researcher",
            source_definition={},
        )
    )
    db_session.commit()
    assert (
        tax.authority_context(
            db_session, entity=entity(), transaction_date=date(2026, 5, 1)
        )["citations"]
        == []
    )


def test_review_requires_independent_reviewer_exact_passages_and_dates(db_session):
    source = research_source(db_session)
    snapshot = capture(db_session, source)
    grant(db_session, "researcher", "review")
    for override, error in [
        ({"reviewed_by": "researcher"}, "independent"),
        (
            {
                "passages": [
                    {"text": "Invented source text is not a fact.", "locator": "p1"}
                ]
            },
            "not_in_capture",
        ),
        ({"effective_from": None}, "dates"),
        ({"valid_until": tax._now() + timedelta(days=91)}, "dates"),
        (
            {
                "applicability": {
                    "jurisdictions": ["*"],
                    "entity_types": ["us_tx_llc"],
                    "topics": ["bookkeeping"],
                }
            },
            "applicability",
        ),
    ]:
        with pytest.raises(ValueError, match=error):
            approve(db_session, snapshot, **override)
        db_session.rollback()
    assert db_session.query(ArkAuthorityReview).count() == 0


def test_successful_review_is_not_rule_promotion(db_session):
    source = research_source(db_session)
    snapshot = capture(db_session, source)
    review = approve(db_session, snapshot)
    bundle = tax.authority_context(
        db_session, entity=entity(), transaction_date=date(2026, 5, 1)
    )
    assert (
        bundle["status"] == "reviewed_reference_only"
        and bundle["automatic_policy_change"] is False
    )
    citation = bundle["citations"][0]
    assert citation["source_sha256"] == sha256(BODY).hexdigest()
    assert citation["passage_sha256"] == sha256(PASSAGE.encode()).hexdigest()
    assert citation["review_id"] == review.id
    assert db_session.query(ArkRuleProposal).one().status == "draft"
    assert snapshot.status == "proposed"
    with pytest.raises(ValueError, match="append-only"):
        snapshot.retrieved_at = tax._now()
        db_session.commit()
    db_session.rollback()


def test_stream_deadline_is_checked_on_small_chunks(monkeypatch):
    ticks = iter((0.0, 1.0, 31.0))
    monkeypatch.setattr(tax, "monotonic", lambda: next(ticks))

    class Slow(httpx.SyncByteStream):
        def __iter__(self):
            yield b"a"
            yield b"b"
            pytest.fail("Deadline must stop before another chunk")

    t = httpx.MockTransport(
        lambda r: httpx.Response(
            200, stream=Slow(), headers={"content-type": "text/plain"}
        )
    )
    with pytest.raises(ValueError, match="deadline"):
        tax.fetch_source("https://www.irs.gov/test", transport=t)


def test_controller_cannot_refresh_global_sources_by_flag(monkeypatch):
    from scripts import run_arkcpa_controller as script
    from argparse import Namespace

    monkeypatch.setattr(
        script,
        "SessionLocal",
        lambda: pytest.fail("Controller must refuse before opening DB"),
    )
    with pytest.raises(PermissionError, match="global research"):
        script.run_cycle(Namespace(refresh_tax=True))


def test_unchanged_refresh_does_not_launder_capture_author(db_session):
    source = research_source(db_session)
    snapshot = capture(db_session, source)
    grant(db_session, "researcher", "review")
    grant(db_session, "another-researcher", "refresh")
    result = tax.refresh_authority_source(
        db_session, source, requested_by="another-researcher", transport=transport()
    )
    assert result["status"] == "unchanged"
    with pytest.raises(ValueError, match="independent_reviewer"):
        approve(db_session, snapshot, reviewed_by="researcher")
