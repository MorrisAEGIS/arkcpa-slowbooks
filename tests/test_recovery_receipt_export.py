import importlib.util
import json
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "receipt_export", Path(__file__).parents[1] / "deploy/arkcpa-receipts.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_export_contains_verification_only_and_removes_invalid_receipt(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    receipt = source / "latest.local.receipt.json"
    receipt.write_text(json.dumps({
        "schema": "arkcpa-recovery-receipt/v2", "run_id": "synthetic",
        "status": "local_verified_remote_pending", "encrypted_archive": "/private/archive",
        "remote_root": "secret-destination", "database_count": 1,
        "verification": {"payload_integrity": "passed", "images_loaded_by_id": "passed",
                         "git_bundle": "passed", "databases": {
                             "private_company_name": {"restore": "passed", "private_path": "/secret"}}}
    }))
    module.export_receipts(source, target)
    output = target / receipt.name
    text = output.read_text()
    assert not any(word in text for word in ("private", "secret", "encrypted_archive", "remote_root"))
    assert json.loads(text)["verification"]["databases"] == {"0": {"restore": "passed"}}
    receipt.write_text("invalid json")
    module.export_receipts(source, target)
    assert not output.exists()


def test_export_does_not_follow_source_symlinks(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    (source / "latest.local.receipt.json").symlink_to(tmp_path / "secret")
    module.export_receipts(source, target)
    assert not list(target.iterdir())
