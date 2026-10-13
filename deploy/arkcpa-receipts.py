"""Export only recovery verification facts to the readonly web receipt mount.

The web process never receives backup archives, recovery keys, configuration,
database names or host paths. Missing/invalid source receipts remove the published
receipt, so a broken exporter cannot manufacture recovery success.
"""

import argparse
import json
import os
from pathlib import Path
import tempfile


def export_receipts(source, destination):
    source, destination = Path(source), Path(destination)
    if destination.is_symlink():
        raise ValueError("Receipt destination must not be a symlink")
    destination.mkdir(mode=0o755, parents=True, exist_ok=True)
    for name in ("latest.local.receipt.json", "latest.remote.receipt.json"):
        target = destination / name
        try:
            original = source / name
            if original.is_symlink() or original.stat().st_size > 128 * 1024:
                raise ValueError("Invalid source receipt")
            value = json.loads(original.read_text())
            if value.get("schema") != "arkcpa-recovery-receipt/v2":
                raise ValueError("Unknown recovery receipt")
            keys = (
                "schema", "run_id", "status", "completed_at", "sha256",
                "source_head", "database_count", "remote_verified",
            )
            safe = {key: value.get(key) for key in keys}
            proof = value["verification"]
            safe["verification"] = {
                key: proof.get(key)
                for key in ("payload_integrity", "images_loaded_by_id", "git_bundle")
            }
            safe["verification"]["databases"] = {
                str(index): {"restore": row.get("restore")}
                for index, row in enumerate(proof["databases"].values())
            }
            payload = json.dumps(safe, sort_keys=True).encode() + b"\n"
            with tempfile.NamedTemporaryFile(dir=destination, delete=False) as output:
                temporary = Path(output.name)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            try:
                temporary.chmod(0o644)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            target.unlink(missing_ok=True)
    # Make the replacements durable before the service reports success.
    directory = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    export_receipts(args.source, args.destination)
