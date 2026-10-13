from datetime import datetime, timedelta, timezone
import json

import pytest

from app.services.arkcpa_recovery_status import recovery_summary


def receipt(**changes):
    return {
        "schema": "arkcpa-recovery-receipt/v2",
        "run_id": "synthetic-run",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "sha256": "a" * 64,
        "source_head": "b" * 40,
        "status": "local_verified_remote_pending",
        "remote_verified": False,
        "database_count": 1,
        "verification": {
            "payload_integrity": "passed",
            "images_loaded_by_id": "passed",
            "git_bundle": "passed",
            "databases": {"private_database": {"restore": "passed"}},
        },
        "encrypted_archive": "/private/archive",
        "remote_archive": "private:remote",
        **changes,
    }


def write(root, name, value):
    (root / name).write_text(json.dumps(value))


def test_unconfigured_does_not_claim_backups(monkeypatch):
    monkeypatch.delenv("ARKCPA_RECOVERY_RECEIPT_DIR", raising=False)
    result = recovery_summary()
    assert result["configured"] is False
    assert result["local"]["status"] == result["offsite"]["status"] == "unavailable"


def test_local_success_cannot_be_masked_by_old_legacy_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("ARKCPA_RECOVERY_RECEIPT_DIR", str(tmp_path))
    write(tmp_path, "latest.local.receipt.json", receipt())
    write(
        tmp_path, "latest.receipt.json", {"status": "passed", "remote_verified": True}
    )
    result = recovery_summary()
    assert result["local"]["status"] == "verified"
    assert result["offsite"]["status"] == "not_verified"
    assert "private" not in json.dumps(result)
    assert "archive" not in json.dumps(result)


def test_remote_must_match_current_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("ARKCPA_RECOVERY_RECEIPT_DIR", str(tmp_path))
    local = receipt()
    write(tmp_path, "latest.local.receipt.json", local)
    remote = {**local, "status": "remote_verified", "remote_verified": True}
    write(tmp_path, "latest.remote.receipt.json", remote)
    assert recovery_summary()["offsite"]["status"] == "verified"
    write(tmp_path, "latest.remote.receipt.json", {**remote, "run_id": "older-run"})
    assert recovery_summary()["offsite"]["status"] == "not_verified"


@pytest.mark.parametrize(
    "changes",
    [
        {"schema": "arkcpa-recovery-receipt/v1"},
        {"verification": {}},
        {"completed_at": "invalid"},
        {"sha256": "invalid"},
        {"completed_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
    ],
)
def test_invalid_receipts_remain_unknown(tmp_path, monkeypatch, changes):
    monkeypatch.setenv("ARKCPA_RECOVERY_RECEIPT_DIR", str(tmp_path))
    write(tmp_path, "latest.local.receipt.json", receipt(**changes))
    assert recovery_summary()["local"]["status"] == "unavailable"


def test_stale_and_symlink_receipts_are_not_current_success(tmp_path, monkeypatch):
    monkeypatch.setenv("ARKCPA_RECOVERY_RECEIPT_DIR", str(tmp_path))
    write(
        tmp_path,
        "latest.local.receipt.json",
        receipt(
            completed_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        ),
    )
    assert recovery_summary()["local"]["status"] == "stale"
    (tmp_path / "latest.remote.receipt.json").symlink_to(
        tmp_path / "latest.local.receipt.json"
    )
    assert recovery_summary()["offsite"]["status"] == "not_verified"
