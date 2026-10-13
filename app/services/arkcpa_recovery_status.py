"""Read only sanitized operator receipts; never run backups from the web app."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

MAX_AGE_SECONDS = 36 * 60 * 60


def _receipt(root: Path, name: str, now: datetime):
    path = root / name
    try:
        # No arbitrary client paths, symlinks, large payloads or legacy v1 success.
        if path.is_symlink() or path.stat().st_size > 128 * 1024:
            return None
        value = json.loads(path.read_text())
        if value.get("schema") != "arkcpa-recovery-receipt/v2":
            return None
        completed = datetime.fromisoformat(value["completed_at"])
        if completed.tzinfo is None or completed > now:
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", value.get("sha256", "")):
            return None
        if not re.fullmatch(r"[0-9a-f]{40}", value.get("source_head", "")):
            return None
        if not isinstance(value.get("run_id"), str):
            return None
        proof = value.get("verification", {})
        if any(
            proof.get(k) != "passed"
            for k in ("payload_integrity", "images_loaded_by_id", "git_bundle")
        ):
            return None
        databases = proof.get("databases", {})
        if (
            not databases
            or len(databases) != value.get("database_count")
            or any(row.get("restore") != "passed" for row in databases.values())
        ):
            return None
        value["completed"] = completed
        return value
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def recovery_summary():
    configured = os.getenv("ARKCPA_RECOVERY_RECEIPT_DIR", "").strip()
    result = {
        "configured": bool(configured),
        "max_age_seconds": MAX_AGE_SECONDS,
        "local": {"status": "unavailable", "verified_at": None},
        "offsite": {"status": "unavailable", "verified_at": None},
    }
    if not configured:
        return result
    now = datetime.now(timezone.utc)
    root = Path(configured)
    local = _receipt(root, "latest.local.receipt.json", now)
    remote = _receipt(root, "latest.remote.receipt.json", now)
    if local and local.get("status") in {
        "local_verified_remote_pending",
        "remote_verified",
    }:
        result["local"] = {
            "status": (
                "stale"
                if (now - local["completed"]).total_seconds() > MAX_AGE_SECONDS
                else "verified"
            ),
            "verified_at": local["completed"].isoformat(),
            "source_revision": local["source_head"],
        }
        result["offsite"]["status"] = "not_verified"
    if (
        remote
        and remote.get("status") == "remote_verified"
        and remote.get("remote_verified") is True
    ):
        result["offsite"]["verified_at"] = remote["completed"].isoformat()
        same_capture = local and all(
            remote.get(k) == local.get(k) for k in ("run_id", "sha256", "source_head")
        )
        if same_capture:
            result["offsite"]["status"] = (
                "stale"
                if (now - remote["completed"]).total_seconds() > MAX_AGE_SECONDS
                else "verified"
            )
    return result
