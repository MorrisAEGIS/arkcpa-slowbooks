"""Bounded, read-only Ark Files extraction for the private Controller.

Raw document bytes and OCR text are transient.  Only hashes, citations, and
small structured facts are persisted; an operator must verify evidence before
it can support a posting decision.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import shutil
import subprocess
from typing import Any

from sqlalchemy.orm import Session

from app.models.arkcpa import (
    ArkEntity,
    ArkEvidence,
    ArkEvidenceFact,
    ArkImportRun,
)
from app.services.ark_files import resolve_evidence_path, scan_entity_files
from app.services.ocr_engines import get_engine
from app.services.ocr_service import extract_receipt
from app.services.pdf_raster import rasterize

EXTRACTOR_VERSION = "arkcpa-evidence-2026-09-10.1"
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_FACTS = 32

_TEXT_EXTENSIONS = {".txt", ".csv", ".json", ".ofx", ".qfx", ".qbo"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
_PROMPT_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior|system)\s+instructions", re.I),
    re.compile(r"(?:system|developer)\s+prompt", re.I),
    re.compile(r"you\s+are\s+(?:now|an?)\b", re.I),
    re.compile(r"(?:reveal|print|exfiltrate).{0,40}(?:secret|password|token|key)", re.I),
    re.compile(r"(?:call|use|invoke)\s+(?:the\s+)?(?:tool|api|shell)", re.I),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text_from_pdf(data: bytes) -> tuple[str, str, int]:
    command = shutil.which("pdftotext")
    if command:
        proc = subprocess.run(
            [command, "-", "-"],
            input=data,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            return (
                proc.stdout[:MAX_TEXT_BYTES].decode("utf-8", errors="replace"),
                "pdftotext",
                0,
            )
    image, page_count = rasterize(data)
    engine = get_engine()
    if not engine.available():
        raise ValueError(engine.unavailable_reason() or "OCR engine unavailable")
    result = engine.recognize(image)
    return result.text[:MAX_TEXT_BYTES], f"{result.engine}-pdf-page-1", page_count


def _extract_text(evidence: ArkEvidence, data: bytes) -> tuple[str, str, int]:
    suffix = "." + evidence.source_path.rsplit(".", 1)[-1].lower()
    if suffix in _TEXT_EXTENSIONS:
        return data[:MAX_TEXT_BYTES].decode("utf-8", errors="replace"), "text", 0
    if suffix == ".pdf":
        return _text_from_pdf(data)
    if suffix in _IMAGE_EXTENSIONS:
        engine = get_engine()
        if not engine.available():
            raise ValueError(engine.unavailable_reason() or "OCR engine unavailable")
        result = engine.recognize(data)
        return result.text[:MAX_TEXT_BYTES], result.engine, 1
    raise ValueError("structured extraction is not available for this file type")


def _contains_embedded_instructions(text: str) -> bool:
    sample = text[:MAX_TEXT_BYTES]
    return any(pattern.search(sample) for pattern in _PROMPT_INJECTION_PATTERNS)


def _confidence(value: str | None) -> float:
    return {"high": 0.96, "low": 0.65, "missing": 0.0}.get(value or "", 0.8)


def _structured_facts(
    evidence: ArkEvidence,
    text: str,
    *,
    engine: str,
    page_count: int,
) -> list[dict[str, Any]]:
    parsed = extract_receipt(text)
    facts: list[dict[str, Any]] = [
        {
            "key": "document.metadata",
            "value": {
                "category": evidence.category,
                "mime_type": evidence.mime_type,
                "size_bytes": evidence.size_bytes,
                "page_count": page_count or None,
                "extraction_engine": engine,
            },
            "confidence": 1.0,
            "locator": "document",
        }
    ]
    merchant = parsed["merchant"]
    candidates = (
        ("receipt.merchant", merchant.get("value"), _confidence(merchant.get("confidence"))),
        ("receipt.date", parsed.get("date"), 0.9),
        ("receipt.total", parsed.get("total"), _confidence(parsed.get("total_confidence"))),
        ("receipt.subtotal", parsed.get("subtotal"), 0.85),
        ("receipt.tax", parsed.get("tax"), 0.85),
        ("receipt.reference", parsed.get("reference"), 0.8),
    )
    for key, value, confidence in candidates:
        if value is not None:
            facts.append(
                {
                    "key": key,
                    "value": {"value": str(value)[:300]},
                    "confidence": confidence,
                    "locator": "page:1" if page_count else "document",
                }
            )
    return facts[:MAX_FACTS]


def _store_facts(
    db: Session,
    evidence: ArkEvidence,
    facts: list[dict[str, Any]],
) -> int:
    db.query(ArkEvidenceFact).filter(
        ArkEvidenceFact.evidence_id == evidence.id,
        ArkEvidenceFact.source_hash != evidence.content_hash,
    ).delete(synchronize_session=False)
    stored = 0
    for fact in facts:
        row = (
            db.query(ArkEvidenceFact)
            .filter(
                ArkEvidenceFact.evidence_id == evidence.id,
                ArkEvidenceFact.fact_key == fact["key"],
                ArkEvidenceFact.locator == fact["locator"],
            )
            .first()
        )
        if row is None:
            row = ArkEvidenceFact(
                evidence_id=evidence.id,
                fact_key=fact["key"],
                locator=fact["locator"],
            )
            db.add(row)
        row.value = fact["value"]
        row.confidence = fact["confidence"]
        row.source_hash = evidence.content_hash
        row.extractor_version = EXTRACTOR_VERSION
        if row.status != "verified":
            row.status = "extracted"
        stored += 1
    return stored


def extract_evidence(db: Session, entity: ArkEntity, evidence: ArkEvidence) -> dict:
    if evidence.entity_id != entity.id:
        raise ValueError("Evidence does not belong to the selected entity")
    if evidence.status not in {"indexed", "extracted", "verified"}:
        return {"status": "skipped", "reason": evidence.quarantine_reason or evidence.status}
    path = resolve_evidence_path(entity, evidence.source_path)
    data = path.read_bytes()
    if sha256(data).hexdigest() != evidence.content_hash:
        evidence.status = "quarantined"
        evidence.quarantine_reason = "file hash changed after indexing"
        db.flush()
        return {"status": "quarantined", "reason": evidence.quarantine_reason}
    try:
        text, engine, page_count = _extract_text(evidence, data)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"status": "unsupported", "reason": str(exc)[:300]}
    text_hash = sha256(text.encode("utf-8", errors="replace")).hexdigest()
    if _contains_embedded_instructions(text):
        evidence.status = "quarantined"
        evidence.quarantine_reason = "possible embedded model instructions"
        evidence.extracted_text_hash = text_hash
        evidence.extractor_version = EXTRACTOR_VERSION
        db.flush()
        return {"status": "quarantined", "reason": evidence.quarantine_reason}
    facts = _structured_facts(
        evidence,
        text,
        engine=engine,
        page_count=page_count,
    )
    stored = _store_facts(db, evidence, facts)
    if evidence.status != "verified":
        evidence.status = "extracted"
    evidence.extracted_text_hash = text_hash
    evidence.extractor_version = EXTRACTOR_VERSION
    evidence.quarantine_reason = None
    db.flush()
    return {"status": evidence.status, "facts": stored, "engine": engine}


def scan_and_extract_entity(db: Session, entity: ArkEntity) -> dict:
    run = ArkImportRun(entity_id=entity.id, kind="ark_files", status="running")
    db.add(run)
    db.commit()
    scan = scan_entity_files(db, entity)
    results = {"extracted": 0, "verified": 0, "quarantined": 0, "unsupported": 0}
    try:
        rows = (
            db.query(ArkEvidence)
            .filter(
                ArkEvidence.entity_id == entity.id,
                ArkEvidence.status.in_(("indexed", "extracted", "verified")),
            )
            .order_by(ArkEvidence.id)
            .all()
        )
        manifest = []
        for evidence in rows:
            outcome = extract_evidence(db, entity, evidence)
            status = outcome["status"]
            results[status if status in results else "unsupported"] += 1
            manifest.append(
                {
                    "path": evidence.source_path,
                    "hash": evidence.content_hash,
                    "status": evidence.status,
                }
            )
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        run.manifest_hash = sha256(encoded).hexdigest()
        run.counts = {**scan, **results}
        run.status = "partial" if results["unsupported"] or results["quarantined"] else "completed"
        run.completed_at = _now()
        db.commit()
    except Exception as exc:
        db.rollback()
        run = db.get(ArkImportRun, run.id)
        run.status = "failed"
        run.error_code = "extraction_failed"
        run.error_message = str(exc)[:500]
        run.completed_at = _now()
        db.commit()
        raise
    return {
        "run_id": run.id,
        "status": run.status,
        "manifest_hash": run.manifest_hash,
        "counts": run.counts,
    }
