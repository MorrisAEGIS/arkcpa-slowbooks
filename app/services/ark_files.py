"""Read-only, fail-closed Ark Files evidence inventory."""

from datetime import datetime, timezone
from hashlib import sha256
import mimetypes
import os
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.arkcpa import ArkEntity, ArkEvidence

ARK_FILES_ROOT = Path(os.getenv("ARK_FILES_ROOT", "/ark-files")).resolve()
MAX_EVIDENCE_BYTES = int(os.getenv("ARKCPA_MAX_EVIDENCE_BYTES", str(50 * 1024 * 1024)))
MAX_SCAN_FILES = int(os.getenv("ARKCPA_MAX_SCAN_FILES", "10000"))
FOLDERS = (
    "Inbox",
    "Statements",
    "Receipts",
    "Sales",
    "Tax",
    "Legal",
    "Exports",
    "Archive",
)
ALLOWED_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".webp",
    ".csv",
    ".ofx",
    ".qfx",
    ".qbo",
    ".xlsx",
    ".xls",
    ".txt",
    ".json",
}


def _safe_entity_root(entity: ArkEntity) -> Path:
    relative = Path(entity.ark_files_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Ark Files entity path must be relative")
    candidate = (ARK_FILES_ROOT / relative).resolve()
    if candidate != ARK_FILES_ROOT and ARK_FILES_ROOT not in candidate.parents:
        raise ValueError("Ark Files entity path escapes the configured root")
    return candidate


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _classification(path: Path, size: int) -> tuple[str, str | None]:
    extension = path.suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        return "quarantined", "unsupported file type"
    if size <= 0:
        return "quarantined", "empty file"
    if size > MAX_EVIDENCE_BYTES:
        return "quarantined", "file exceeds evidence size limit"
    try:
        with path.open("rb") as handle:
            signature = handle.read(8)
    except OSError:
        return "quarantined", "file could not be read"
    if signature.startswith((b"MZ", b"\x7fELF")):
        return "quarantined", "executable content is not accepted as evidence"
    if extension == ".pdf" and not signature.startswith(b"%PDF-"):
        return "quarantined", "file signature does not match PDF extension"
    return "indexed", None


def resolve_evidence_path(entity: ArkEntity, source_path: str) -> Path:
    """Resolve an indexed source path inside one entity's read-only folder."""
    relative = Path(source_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Evidence path must be relative")
    unresolved = ARK_FILES_ROOT / relative
    if unresolved.is_symlink() or any(
        parent.is_symlink()
        for parent in unresolved.parents
        if parent != ARK_FILES_ROOT and ARK_FILES_ROOT in parent.parents
    ):
        raise ValueError("Evidence path cannot contain symlinks")
    candidate = unresolved.resolve(strict=True)
    entity_root = _safe_entity_root(entity)
    if candidate == entity_root or entity_root not in candidate.parents:
        raise ValueError("Evidence path is outside the entity folder")
    if not candidate.is_file():
        raise ValueError("Evidence path is not a regular file")
    return candidate


def scan_entity_files(db: Session, entity: ArkEntity) -> dict:
    """Inventory one entity folder without following links or reading raw content."""
    root = _safe_entity_root(entity)
    if not root.is_dir():
        return {
            "root": str(root),
            "scanned": 0,
            "indexed": 0,
            "quarantined": 0,
            "missing": True,
        }

    scanned = indexed = quarantined = 0
    for folder in FOLDERS:
        folder_path = root / folder
        if not folder_path.is_dir():
            continue
        for walk_root, dirs, files in os.walk(folder_path, followlinks=False):
            dirs[:] = sorted(
                name for name in dirs if not (Path(walk_root) / name).is_symlink()
            )
            for filename in sorted(files):
                if scanned >= MAX_SCAN_FILES:
                    db.commit()
                    return {
                        "root": str(root),
                        "scanned": scanned,
                        "indexed": indexed,
                        "quarantined": quarantined,
                        "truncated": True,
                    }
                path = Path(walk_root) / filename
                if path.is_symlink() or not path.is_file():
                    continue
                scanned += 1
                stat = path.stat()
                status, reason = _classification(path, stat.st_size)
                content_hash = _hash_file(path) if status == "indexed" else None
                after = path.stat()
                if status == "indexed" and (
                    after.st_size != stat.st_size
                    or after.st_mtime_ns != stat.st_mtime_ns
                ):
                    status, reason, content_hash = (
                        "quarantined",
                        "file changed while it was being hashed",
                        None,
                    )
                source_path = path.relative_to(ARK_FILES_ROOT).as_posix()
                record = (
                    db.query(ArkEvidence)
                    .filter(
                        ArkEvidence.entity_id == entity.id,
                        ArkEvidence.source_path == source_path,
                    )
                    .first()
                )
                if record is None:
                    record = ArkEvidence(entity_id=entity.id, source_path=source_path)
                    db.add(record)
                prior_hash = record.content_hash
                prior_status = record.status
                record.category = folder
                record.content_hash = content_hash
                record.size_bytes = stat.st_size
                record.mime_type = (
                    mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                )
                record.status = (
                    "verified"
                    if status == "indexed"
                    and prior_status == "verified"
                    and prior_hash == content_hash
                    else status
                )
                record.quarantine_reason = reason
                record.modified_at = datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                )
                if status == "indexed":
                    indexed += 1
                else:
                    quarantined += 1
    db.commit()
    return {
        "root": str(root),
        "scanned": scanned,
        "indexed": indexed,
        "quarantined": quarantined,
        "truncated": False,
    }
