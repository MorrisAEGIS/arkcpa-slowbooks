"""Official-source change monitor for Ark CPA tax research.

This service detects source changes; it never silently turns web content into
live tax policy.  Every change becomes a proposal that requires tests and the
protected owner approval path before promotion.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import os
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from app.models.arkcpa import ArkAuthoritySource, ArkRuleProposal, ArkSourceSnapshot

MAX_SOURCE_BYTES = 5 * 1024 * 1024
SOURCE_USER_AGENT = "Ark-CPA-tax-authority-monitor/1"

DEFAULT_AUTHORITY_SOURCES = (
    {
        "code": "CA_INCOME_TAX_ACT",
        "title": "Income Tax Act",
        "jurisdiction": "Canada",
        "authority_level": "law",
        "url": "https://laws-lois.justice.gc.ca/eng/acts/I-3.3/",
    },
    {
        "code": "CRA_CORPORATION_T2",
        "title": "Corporation income tax return guidance",
        "jurisdiction": "Canada",
        "authority_level": "administrative",
        "url": "https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/corporations/corporation-income-tax-return.html",
    },
    {
        "code": "CRA_TRUST_T3",
        "title": "Trust income tax and information return guidance",
        "jurisdiction": "Canada",
        "authority_level": "administrative",
        "url": "https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/t4013/t3-trust-guide.html",
    },
    {
        "code": "AB_CORPORATE_INCOME_TAX",
        "title": "Alberta corporate income tax",
        "jurisdiction": "Canada / Alberta",
        "authority_level": "administrative",
        "url": "https://www.alberta.ca/corporate-income-tax",
    },
    {
        "code": "TX_FRANCHISE_TAX",
        "title": "Texas franchise tax",
        "jurisdiction": "United States / Texas",
        "authority_level": "administrative",
        "url": "https://comptroller.texas.gov/taxes/franchise/",
    },
    {
        "code": "IRS_FORM_5472",
        "title": "Instructions for Form 5472",
        "jurisdiction": "United States",
        "authority_level": "administrative",
        "url": "https://www.irs.gov/instructions/i5472",
    },
    {
        "code": "FINCEN_BOI",
        "title": "Beneficial ownership information reporting",
        "jurisdiction": "United States",
        "authority_level": "administrative",
        "url": "https://www.fincen.gov/boi",
    },
)

_DEFAULT_HOSTS = {
    "laws-lois.justice.gc.ca",
    "www.canada.ca",
    "www.alberta.ca",
    "comptroller.texas.gov",
    "www.irs.gov",
    "www.fincen.gov",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def seed_authority_sources(db: Session) -> int:
    created = 0
    for definition in DEFAULT_AUTHORITY_SOURCES:
        row = db.query(ArkAuthoritySource).filter_by(code=definition["code"]).first()
        if row is None:
            row = ArkAuthoritySource(**definition, license_note="Official public authority")
            db.add(row)
            created += 1
        else:
            row.title = definition["title"]
            row.jurisdiction = definition["jurisdiction"]
            row.authority_level = definition["authority_level"]
            row.url = definition["url"]
    db.commit()
    return created


def _allowed_hosts() -> set[str]:
    configured = {
        value.strip().lower()
        for value in os.getenv("ARKCPA_TAX_SOURCE_HOSTS", "").split(",")
        if value.strip()
    }
    return _DEFAULT_HOSTS | configured


def _validate_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or parsed.hostname.lower() not in _allowed_hosts()
    ):
        raise ValueError("Authority source URL is outside the HTTPS allowlist")
    return url


def fetch_source(url: str, client: httpx.Client | None = None) -> tuple[bytes, dict]:
    safe_url = _validate_url(url)
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=30,
            verify=True,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": SOURCE_USER_AGENT, "Accept": "text/html,application/pdf"},
        )
    try:
        response = client.get(safe_url)
        if response.status_code != 200:
            raise ValueError(f"Authority source returned HTTP {response.status_code}")
        content = response.content
        if not content or len(content) > MAX_SOURCE_BYTES:
            raise ValueError("Authority source body is empty or exceeds the size limit")
        return content, {
            "content_type": response.headers.get("content-type", "")[:120],
            "etag": response.headers.get("etag", "")[:240],
            "last_modified": response.headers.get("last-modified", "")[:120],
            "size_bytes": len(content),
        }
    except httpx.HTTPError as exc:
        raise ValueError("Authority source request failed") from exc
    finally:
        if owns_client:
            client.close()


def refresh_authority_source(
    db: Session,
    source: ArkAuthoritySource,
    *,
    client: httpx.Client | None = None,
) -> dict:
    content, metadata = fetch_source(source.url, client=client)
    content_hash = sha256(content).hexdigest()
    current = (
        db.query(ArkSourceSnapshot)
        .filter_by(source_id=source.id, status="current")
        .order_by(ArkSourceSnapshot.retrieved_at.desc())
        .first()
    )
    source.last_checked_at = _now()
    if current and current.content_hash == content_hash:
        current.retrieved_at = _now()
        current.snapshot_metadata = metadata
        db.commit()
        return {"source": source.code, "status": "unchanged", "hash": content_hash}
    previous_hash = current.content_hash if current else None
    snapshot = ArkSourceSnapshot(
        source_id=source.id,
        content_hash=content_hash,
        status="proposed" if current else "current",
        snapshot_metadata=metadata,
    )
    db.add(snapshot)
    db.flush()
    proposal_id = None
    if current:
        proposal = ArkRuleProposal(
            source_snapshot_id=snapshot.id,
            code=f"SOURCE_CHANGE_{source.code}_{content_hash[:12]}",
            title=f"Review detected change to {source.title}",
            proposed_change={
                "source_code": source.code,
                "previous_hash": previous_hash,
                "candidate_hash": content_hash,
                "automatic_policy_change": False,
            },
            status="draft",
            test_results={"status": "not_run"},
        )
        db.add(proposal)
        db.flush()
        proposal_id = proposal.id
    db.commit()
    return {
        "source": source.code,
        "status": "change_detected" if current else "baseline_created",
        "hash": content_hash,
        "proposal_id": proposal_id,
    }


def refresh_all_authority_sources(
    db: Session, *, client: httpx.Client | None = None
) -> list[dict]:
    seed_authority_sources(db)
    results = []
    for source in (
        db.query(ArkAuthoritySource)
        .filter(ArkAuthoritySource.enabled)
        .order_by(ArkAuthoritySource.code)
        .all()
    ):
        try:
            results.append(refresh_authority_source(db, source, client=client))
        except ValueError as exc:
            db.rollback()
            source = db.get(ArkAuthoritySource, source.id)
            source.last_checked_at = _now()
            db.commit()
            results.append({"source": source.code, "status": "failed", "error": str(exc)})
    return results
