#!/usr/bin/env python3
"""Pinned CPA recovery: complete encrypted capture and an isolated restore drill.

This replaces the external six-database shell wrapper after explicit deployment.
No production database is restored and no service is restarted. Configuration is
an operator-owned JSON file containing paths/container IDs, never secret values.
"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone


class RecoveryError(RuntimeError):
    pass


def command(args, *, stdin=None, stdout=None, cwd=None):
    result = subprocess.run(
        args,
        stdin=stdin,
        stdout=stdout or subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        check=False,
    )
    if result.returncode:
        # Do not expose DB contents, expanded configuration or OAuth diagnostics.
        raise RecoveryError(f"{args[0]} operation failed (exit {result.returncode})")
    return result.stdout.decode() if result.stdout is not None else ""


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, data):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("x") as stream:
        os.chmod(temp, 0o600)
        json.dump(data, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def regular_files(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise RecoveryError("source must be a real directory")
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            p = Path(base) / name
            if p.is_symlink() or not (p.is_file() or p.is_dir()):
                raise RecoveryError("symlinks and special files are not accepted")
        for name in files:
            yield Path(base) / name


def tree_manifest(root):
    return {
        str(p.relative_to(root)): {"bytes": p.stat().st_size, "sha256": sha(p)}
        for p in sorted(regular_files(root))
    }


def copy_tree(source, destination):
    before = tree_manifest(source)
    shutil.copytree(source, destination)
    if tree_manifest(source) != before or tree_manifest(destination) != before:
        raise RecoveryError("source files changed during capture")
    return {"files": len(before), "bytes": sum(p["bytes"] for p in before.values())}


def safe_extract(archive, destination):
    """Only regular files/directories inside one recovery root are admissible."""
    with tarfile.open(archive, "r:") as source:
        seen = set()
        members = source.getmembers()
        for item in members:
            p = Path(item.name)
            if (
                p.is_absolute()
                or ".." in p.parts
                or not p.parts
                or p.parts[0] != "recovery"
                or item.name in seen
                or not (item.isfile() or item.isdir())
            ):
                raise RecoveryError("unsafe recovery archive member")
            seen.add(item.name)
        for item in members:
            target = Path(destination) / item.name
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with source.extractfile(item) as src, target.open("xb") as dst:
                    os.chmod(target, 0o600)
                    shutil.copyfileobj(src, dst)


def validate_config(config):
    for name in (
        "project_dir",
        "source_dir",
        "ark_files_root",
        "config_root",
        "key_file",
        "gpg_home",
        "output_root",
        "work_root",
    ):
        if not Path(config[name]).is_absolute():
            raise RecoveryError(f"{name} must be absolute")
    key = Path(config["key_file"])
    if key.is_symlink() or not stat.S_ISREG(key.stat().st_mode):
        raise RecoveryError("key must be a regular file")
    if stat.S_IMODE(key.stat().st_mode) != 0o600:
        raise RecoveryError("key permissions must be 0600")
    key_path = key.resolve()
    project = Path(config["project_dir"])
    config_root = Path(config["config_root"])
    captured_trees = [
        Path(config["ark_files_root"]).resolve(),
        Path(config["source_dir"]).resolve(),
        (config_root / "import-manifests").resolve(),
    ]
    captured_files = [
        project / ".env",
        project / "docker-compose.ark.yml",
        *[
            config_root / name
            for name in (
                "arkcpa-entities-jay-only.json",
                "entity-discovery.json",
                "onedrive-manifest.json",
                "import_arkcpa_from_manifest.py",
            )
        ],
    ]
    if any(
        key_path == root or root in key_path.parents for root in captured_trees
    ) or any(key_path == path.resolve() for path in captured_files):
        raise RecoveryError("encryption passphrase must be outside captured sources")
    prefix = config.get("database_prefix", "arkcpa")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", prefix):
        raise RecoveryError("invalid governed database prefix")
    for kind in ("postgres", "app", "controller"):
        pin = config["containers"][kind]
        if not re.fullmatch(r"[0-9a-f]{64}", pin["id"]):
            raise RecoveryError("container pins must be complete IDs")
    # Prevent recursively capturing our own staging/output or encryption key.
    inputs = [
        Path(config[k]).resolve()
        for k in ("project_dir", "source_dir", "ark_files_root", "config_root")
    ]
    outputs = [Path(config[k]).resolve() for k in ("output_root", "work_root")]
    for out in outputs:
        if any(
            out == src or src in out.parents or out in src.parents for src in inputs
        ):
            raise RecoveryError("backup input and output directories must be disjoint")
    if outputs[0] == outputs[1] or any(
        a in b.parents for a, b in (outputs, outputs[::-1])
    ):
        raise RecoveryError("encrypted output and plaintext staging must be separate")
    remote = config.get("remote_root")
    if remote and not re.match(r"^[a-zA-Z0-9_-]+:[^/]", remote):
        raise RecoveryError("remote_root must name an explicit rclone remote")


def inspect_pins(config):
    result = {}
    for kind, pin in config["containers"].items():
        info = json.loads(command(["docker", "inspect", pin["name"]]))[0]
        if info["Id"] != pin["id"] or info["State"]["Status"] != "running":
            raise RecoveryError("container identity/state changed")
        result[kind] = {"id": info["Id"], "image": info["Image"]}
    return result


def private_runtime_descriptors(config):
    """Full runtime configuration; only write this inside the encrypted payload."""
    result = {}
    for kind, pin in config["containers"].items():
        info = json.loads(command(["docker", "inspect", pin["name"]]))[0]
        if info["Id"] != pin["id"] or info["State"]["Status"] != "running":
            raise RecoveryError("container identity/state changed")
        result[kind] = info
    return result


def runtime_contract(descriptors):
    # Docker serializes Mounts from an unordered map; order can differ on back-to-
    # back inspect calls. Preserve every mount field but canonicalize that list.
    # State/health counters are observations, not effective configuration.
    stable = {}
    for kind, item in descriptors.items():
        stable[kind] = {
            key: item[key] for key in ("Id", "Image", "Config", "HostConfig")
        }
        stable[kind]["Mounts"] = sorted(
            item["Mounts"], key=lambda mount: json.dumps(mount, sort_keys=True)
        )
        stable[kind]["Networks"] = item.get("NetworkSettings", {}).get("Networks", {})
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def sql(container, database, query):
    return command(
        [
            "docker",
            "exec",
            container,
            "sh",
            "-c",
            'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -Atc "$2"',
            "sh",
            database,
            query,
        ]
    ).strip()


def inventory(container, prefix):
    query = (
        "SELECT datname FROM pg_database WHERE NOT datistemplate AND "
        f"(datname='{prefix}' OR left(datname,{len(prefix) + 1})='{prefix}_') ORDER BY datname"
    )
    names = sql(container, "postgres", query).splitlines()
    if prefix not in names or any(
        not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", n) for n in names
    ):
        raise RecoveryError("missing control database or invalid database name")
    return names


def verify_image_archive(path, expected):
    with tarfile.open(path, "r:") as archive:
        items = json.load(archive.extractfile("manifest.json"))
        found = set()
        for item in items:
            # Saving by ID avoids retagging services when a bundle is loaded.
            if item.get("RepoTags"):
                raise RecoveryError("recovery image archive unexpectedly contains tags")
            with archive.extractfile(item["Config"]) as stream:
                found.add("sha256:" + hashlib.sha256(stream.read()).hexdigest())
        if found != set(expected):
            raise RecoveryError("image archive does not match pinned image IDs")


def restore_databases(payload, image):
    name = "cpa-recovery-drill-" + uuid.uuid4().hex
    ident = command(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            "--label",
            "org.ark.cpa.recovery=" + name,
            "--tmpfs",
            "/var/lib/postgresql/data:rw,noexec,nosuid,size=1g",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "-e",
            "POSTGRES_USER=postgres",
            image,
        ]
    ).strip()
    proof = {}
    try:
        for _ in range(60):
            try:
                sql(ident, "postgres", "SELECT 1")
                break
            except RecoveryError:
                time.sleep(0.5)
        else:
            raise RecoveryError("isolated PostgreSQL did not become ready")
        manifest = json.loads((payload / "manifest.json").read_text())
        for database in manifest["databases"]:
            command(["docker", "exec", ident, "createdb", "-U", "postgres", database])
            with (payload / "databases" / (database + ".dump")).open("rb") as source:
                command(
                    [
                        "docker",
                        "exec",
                        "-i",
                        ident,
                        "pg_restore",
                        "--exit-on-error",
                        "-U",
                        "postgres",
                        "-d",
                        database,
                        "--no-owner",
                        "--no-privileges",
                    ],
                    stdin=source,
                )
            tables = int(
                sql(
                    ident,
                    database,
                    "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname='public'",
                )
            )
            if tables != manifest["table_counts"][database]:
                raise RecoveryError("restored table count differs from captured source")
            proof[database] = {"tables": tables, "restore": "passed"}
    finally:
        # Only the random container created above is stopped. No Compose command.
        command(["docker", "stop", ident])
    return proof


def gpg(config, args, **kwargs):
    return command(
        [
            "gpg",
            "--batch",
            "--yes",
            "--homedir",
            config["gpg_home"],
            "--pinentry-mode",
            "loopback",
            "--passphrase-file",
            config["key_file"],
            *args,
        ],
        **kwargs,
    )


def verify(config, encrypted):
    with tempfile.TemporaryDirectory(
        prefix="cpa-verify-", dir=config["work_root"]
    ) as work:
        work = Path(work)
        tar_path = work / "decrypted.tar"
        gpg(config, ["--output", str(tar_path), "--decrypt", str(encrypted)])
        safe_extract(tar_path, work / "extracted")
        payload = work / "extracted" / "recovery"
        expected = json.loads((payload / "integrity.json").read_text())
        actual = tree_manifest(payload)
        actual.pop("integrity.json")
        if actual != expected:
            raise RecoveryError("decrypted payload integrity mismatch")
        manifest = json.loads((payload / "manifest.json").read_text())
        verify_image_archive(payload / "images.tar", manifest["image_ids"])
        command(["docker", "load", "--input", str(payload / "images.tar")])
        command(
            ["git", "bundle", "verify", str(payload / "source.bundle")],
            cwd=config["source_dir"],
        )
        restore = restore_databases(
            payload, manifest["containers"]["postgres"]["image"]
        )
        return {
            "payload_integrity": "passed",
            "images_loaded_by_id": "passed",
            "git_bundle": "passed",
            "databases": restore,
        }


def backup(config):
    validate_config(config)
    os.umask(0o077)
    for key in ("output_root", "work_root", "gpg_home"):
        Path(config[key]).mkdir(parents=True, exist_ok=True, mode=0o700)
        if Path(config[key]).is_symlink():
            raise RecoveryError("output directories may not be symlinks")
        os.chmod(config[key], 0o700)
    with (Path(config["work_root"]) / "backup.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RecoveryError("another recovery run is active") from error
        return capture(config)


def capture(config):
    pins = inspect_pins(config)
    pg = pins["postgres"]["id"]
    prefix = config.get("database_prefix", "arkcpa")
    databases = inventory(pg, prefix)
    project = Path(config["project_dir"])
    source_repo = Path(config["source_dir"])
    if command(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=source_repo
    ).strip():
        raise RecoveryError("source repository must be clean before recovery capture")
    head = command(["git", "rev-parse", "HEAD"], cwd=source_repo).strip()
    if config.get("expected_source_head", head) != head:
        raise RecoveryError("source revision differs from selected recovery pin")
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    encrypted = Path(config["output_root"]) / ("arkcpa-recovery-" + run_id + ".tar.gpg")
    with tempfile.TemporaryDirectory(
        prefix="cpa-capture-", dir=config["work_root"]
    ) as work:
        work = Path(work)
        payload = work / "recovery"
        for subdir in ("databases", "config", "uploads"):
            (payload / subdir).mkdir(parents=True, mode=0o700)
        private_runtime = private_runtime_descriptors(config)
        runtime_file = payload / "config" / "container-runtime.private.json"
        write_json(runtime_file, private_runtime)
        runtime_sha = sha(runtime_file)
        counts = {}
        for database in databases:
            counts[database] = int(
                sql(
                    pg,
                    database,
                    "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname='public'",
                )
            )
            with (payload / "databases" / (database + ".dump")).open("wb") as output:
                command(
                    [
                        "docker",
                        "exec",
                        pg,
                        "sh",
                        "-c",
                        'exec pg_dump -U "$POSTGRES_USER" -d "$1" -Fc --no-owner --no-privileges',
                        "sh",
                        database,
                    ],
                    stdout=output,
                )
        with (payload / "databases" / "globals.sql").open("wb") as output:
            command(
                [
                    "docker",
                    "exec",
                    pg,
                    "sh",
                    "-c",
                    'exec pg_dumpall -U "$POSTGRES_USER" --globals-only --no-role-passwords',
                ],
                stdout=output,
            )
        config_hashes = {}
        for source, name in (
            (project / ".env", "arkcpa.env"),
            (project / "docker-compose.ark.yml", "docker-compose.ark.yml"),
        ):
            if source.is_symlink() or not source.is_file():
                raise RecoveryError("deployment input is missing or symlinked")
            config_hashes[str(source)] = sha(source)
            shutil.copyfile(source, payload / "config" / name)
            if sha(payload / "config" / name) != config_hashes[str(source)]:
                raise RecoveryError("deployment input changed during capture")
        config_root = Path(config["config_root"])
        for name in (
            "arkcpa-entities-jay-only.json",
            "entity-discovery.json",
            "onedrive-manifest.json",
            "import_arkcpa_from_manifest.py",
        ):
            source = config_root / name
            if source.exists():
                if source.is_symlink() or not source.is_file():
                    raise RecoveryError("unsafe configuration source")
                config_hashes[str(source)] = sha(source)
                shutil.copyfile(source, payload / "config" / name)
                if sha(payload / "config" / name) != config_hashes[str(source)]:
                    raise RecoveryError("configuration changed during capture")
        if (config_root / "import-manifests").exists():
            copy_tree(
                config_root / "import-manifests",
                payload / "config" / "import-manifests",
            )
        evidence = copy_tree(Path(config["ark_files_root"]), payload / "ark-files")
        command(
            [
                "docker",
                "cp",
                pins["app"]["id"] + ":/app/app/static/uploads/.",
                str(payload / "uploads"),
            ]
        )
        uploads = tree_manifest(payload / "uploads")
        command(
            ["git", "bundle", "create", str(payload / "source.bundle"), "HEAD"],
            cwd=source_repo,
        )
        images = sorted({p["image"] for p in pins.values()})
        command(
            [
                "docker",
                "image",
                "save",
                "--output",
                str(payload / "images.tar"),
                *images,
            ]
        )
        verify_image_archive(payload / "images.tar", images)
        if inventory(pg, prefix) != databases or inspect_pins(config) != pins:
            raise RecoveryError(
                "database inventory or service identity changed during capture"
            )
        if runtime_contract(private_runtime_descriptors(config)) != runtime_contract(
            private_runtime
        ):
            raise RecoveryError("runtime configuration changed during capture")
        if (
            command(["git", "rev-parse", "HEAD"], cwd=source_repo).strip() != head
            or command(
                ["git", "status", "--porcelain", "--untracked-files=normal"],
                cwd=source_repo,
            ).strip()
        ):
            raise RecoveryError("source revision changed during capture")
        if any(sha(path) != expected for path, expected in config_hashes.items()):
            raise RecoveryError(
                "deployment configuration changed before capture finished"
            )
        if tree_manifest(Path(config["ark_files_root"])) != tree_manifest(
            payload / "ark-files"
        ):
            raise RecoveryError("evidence changed before capture finished")
        if (config_root / "import-manifests").exists() and tree_manifest(
            config_root / "import-manifests"
        ) != tree_manifest(payload / "config" / "import-manifests"):
            raise RecoveryError("import manifests changed before capture finished")
        upload_check = work / "uploads-recheck"
        upload_check.mkdir()
        command(
            [
                "docker",
                "cp",
                pins["app"]["id"] + ":/app/app/static/uploads/.",
                str(upload_check),
            ]
        )
        if tree_manifest(upload_check) != uploads:
            raise RecoveryError("uploads changed before capture finished")
        manifest = {
            "schema": "arkcpa-recovery/v2",
            "run_id": run_id,
            "source_head": head,
            "runtime_descriptor_sha256": runtime_sha,
            "source_role": config.get(
                "source_role",
                "operator-selected source; runtime image IDs captured independently",
            ),
            "containers": pins,
            "image_ids": images,
            "databases": databases,
            "table_counts": counts,
            "ark_files": evidence,
            "uploads": {
                "files": len(uploads),
                "bytes": sum(x["bytes"] for x in uploads.values()),
            },
            "consistency": "Each pg_dump is transaction-consistent; cross-database and file/application quiescence is an operator release gate.",
        }
        write_json(payload / "manifest.json", manifest)
        write_json(payload / "integrity.json", tree_manifest(payload))
        tar_path = work / "payload.tar"
        with tarfile.open(tar_path, "w") as archive:
            archive.add(payload, arcname="recovery", recursive=True)
        gpg(
            config,
            [
                "--symmetric",
                "--cipher-algo",
                "AES256",
                "--output",
                str(encrypted),
                str(tar_path),
            ],
        )
    proof = verify(config, encrypted)
    receipt = {
        "schema": "arkcpa-recovery-receipt/v2",
        "run_id": run_id,
        "status": "local_verified_remote_pending",
        "encrypted_archive": str(encrypted),
        "sha256": sha(encrypted),
        "bytes": encrypted.stat().st_size,
        "database_count": len(databases),
        "ark_files": manifest["ark_files"],
        "source_head": head,
        "runtime_descriptor_sha256": runtime_sha,
        "image_ids": images,
        "verification": proof,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "remote_verified": False,
    }
    receipt_path = encrypted.with_suffix(".receipt.json")
    write_json(receipt_path, receipt)
    write_json(Path(config["output_root"]) / "latest.local.receipt.json", receipt)
    remote_root = config.get("remote_root")
    if remote_root:
        remote = remote_root.rstrip("/") + "/" + encrypted.name
        try:
            command(["rclone", "copyto", str(encrypted), remote, "--retries", "3"])
            with tempfile.TemporaryDirectory(
                prefix="cpa-remote-", dir=config["work_root"]
            ) as tmp:
                fetched = Path(tmp) / "ciphertext.gpg"
                command(["rclone", "copyto", remote, str(fetched), "--retries", "3"])
                if sha(fetched) != receipt["sha256"]:
                    raise RecoveryError("remote ciphertext checksum mismatch")
        except RecoveryError:
            write_json(
                encrypted.with_suffix(".remote-failure.json"),
                {"run_id": run_id, "local_verified": True, "remote_verified": False},
            )
            raise
        receipt.update(
            status="remote_verified", remote_verified=True, remote_archive=remote
        )
        write_json(receipt_path, receipt)
        write_json(Path(config["output_root"]) / "latest.remote.receipt.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "backup", "verify"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text())
        validate_config(config)
        if args.action == "plan":
            result = {
                "config_valid": True,
                "containers_pinned": inspect_pins(config),
                "remote_requested": bool(config.get("remote_root")),
                "writes_performed": False,
            }
        elif args.action == "verify":
            if not args.archive:
                parser.error("verify requires --archive")
            result = verify(config, args.archive)
        else:
            result = backup(config)
        print(json.dumps(result, indent=2))
    except (RecoveryError, OSError, KeyError, ValueError) as error:
        # Deliberately omit exception details that could contain private paths.
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_type": type(error).__name__,
                    "detail": (
                        str(error)
                        if isinstance(error, RecoveryError)
                        else "configuration or filesystem failure"
                    ),
                }
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
