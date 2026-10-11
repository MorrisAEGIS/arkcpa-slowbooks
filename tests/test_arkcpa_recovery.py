"""Recovery boundaries: unsafe content, inventory changes and false receipts."""

import importlib.util
import copy
import io
import json
from pathlib import Path
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "recovery", ROOT / "scripts/arkcpa_recovery.py"
)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


@pytest.fixture
def runtime_descriptor():
    return {
        "app": {
            "Id": "a" * 64,
            "Image": "sha256:" + "b" * 64,
            "State": {
                "Status": "running",
                "Health": {"Status": "healthy", "Log": [{"Output": "old"}]},
            },
            "Config": {"Env": ["PRIVATE=synthetic"], "Cmd": ["serve"]},
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
            "Mounts": [
                {"Source": "/first", "Destination": "/uploads", "RW": True},
                {"Source": "/second", "Destination": "/ark-files", "RW": False},
            ],
            "NetworkSettings": {
                "Networks": {"private": {"NetworkID": "network-a", "Aliases": ["app"]}}
            },
        }
    }


def test_runtime_fingerprint_allows_health_and_mount_order_noise(runtime_descriptor):
    before = recovery.runtime_contract(runtime_descriptor)
    changed = copy.deepcopy(runtime_descriptor)
    changed["app"]["State"]["Health"]["Log"].append(
        {"Output": "new health observation"}
    )
    changed["app"]["State"]["Health"]["Status"] = "starting"
    changed["app"]["Mounts"].reverse()
    assert recovery.runtime_contract(changed) == before


@pytest.mark.parametrize(
    "mutation", ["env", "mount", "identity", "image", "network", "restart"]
)
def test_runtime_fingerprint_rejects_effective_change(runtime_descriptor, mutation):
    before = recovery.runtime_contract(runtime_descriptor)
    changed = copy.deepcopy(runtime_descriptor)
    app = changed["app"]
    if mutation == "env":
        app["Config"]["Env"] = ["PRIVATE=different"]
    elif mutation == "mount":
        app["Mounts"][0]["Source"] = "/different-source"
    elif mutation == "identity":
        app["Id"] = "c" * 64
    elif mutation == "image":
        app["Image"] = "sha256:" + "d" * 64
    elif mutation == "network":
        app["NetworkSettings"]["Networks"]["private"]["NetworkID"] = "different-network"
    else:
        app["HostConfig"]["RestartPolicy"]["Name"] = "no"
    assert recovery.runtime_contract(changed) != before


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../outside", "file"),
        ("/outside", "file"),
        ("recovery/x", "symlink"),
        ("recovery/x", "hardlink"),
        ("recovery/x", "device"),
    ],
)
def test_archive_escape_and_links_rejected(tmp_path, name, kind):
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as out:
        item = tarfile.TarInfo(name)
        if kind != "file":
            item.type = {
                "symlink": tarfile.SYMTYPE,
                "hardlink": tarfile.LNKTYPE,
                "device": tarfile.CHRTYPE,
            }[kind]
            item.linkname = "/etc/passwd"
        out.addfile(item, io.BytesIO())
    with pytest.raises(recovery.RecoveryError, match="unsafe"):
        recovery.safe_extract(archive, tmp_path / "restore")
    assert not (tmp_path / "outside").exists()


def test_archive_bytes_roundtrip(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "invoice.bin").write_bytes(b"synthetic evidence\x00\xff")
    archive = tmp_path / "good.tar"
    with tarfile.open(archive, "w") as out:
        out.add(source, arcname="recovery")
    recovery.safe_extract(archive, tmp_path / "restore")
    assert recovery.tree_manifest(source) == recovery.tree_manifest(
        tmp_path / "restore/recovery"
    )


def test_copy_refuses_evidence_symlink(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "outside").symlink_to("/etc/passwd")
    with pytest.raises(recovery.RecoveryError, match="symlinks"):
        recovery.copy_tree(source, tmp_path / "copy")


def test_copy_detects_mutating_evidence(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "invoice").write_text("before")
    original = recovery.shutil.copytree

    def changing_copy(src, dst):
        original(src, dst)
        (src / "invoice").write_text("changed")

    monkeypatch.setattr(recovery.shutil, "copytree", changing_copy)
    with pytest.raises(recovery.RecoveryError, match="changed"):
        recovery.copy_tree(source, tmp_path / "copy")


def test_dynamic_inventory_includes_new_enerteq_database(monkeypatch):
    monkeypatch.setattr(
        recovery, "sql", lambda *args: "arkcpa\narkcpa_enerteq\narkcpa_maga_energy"
    )
    assert recovery.inventory("synthetic", "arkcpa") == [
        "arkcpa",
        "arkcpa_enerteq",
        "arkcpa_maga_energy",
    ]


@pytest.mark.parametrize(
    "names", ["", "arkcpa_extra", "arkcpa\n../outside", "arkcpa\nquote'bad"]
)
def test_inventory_fails_closed(monkeypatch, names):
    monkeypatch.setattr(recovery, "sql", lambda *args: names)
    with pytest.raises(recovery.RecoveryError):
        recovery.inventory("synthetic", "arkcpa")


def test_subprocess_error_hides_private_stderr(monkeypatch):
    class Result:
        returncode = 1
        stderr = b"private credential material"
        stdout = b"private financial data"

    monkeypatch.setattr(recovery.subprocess, "run", lambda *args, **kwargs: Result())
    with pytest.raises(recovery.RecoveryError) as error:
        recovery.command(["docker", "example"])
    assert "private" not in str(error.value)


def test_image_bundle_may_not_retag_running_services(tmp_path):
    archive = tmp_path / "images.tar"
    with tarfile.open(archive, "w") as out:
        data = json.dumps(
            [{"RepoTags": ["production:latest"], "Config": "a.json"}]
        ).encode()
        item = tarfile.TarInfo("manifest.json")
        item.size = len(data)
        out.addfile(item, io.BytesIO(data))
    with pytest.raises(recovery.RecoveryError, match="tags"):
        recovery.verify_image_archive(archive, ["sha256:" + "a" * 64])


def test_config_requires_private_key_and_disjoint_staging(tmp_path):
    key = tmp_path / "key"
    key.write_text("synthetic only")
    key.chmod(0o600)
    config = {
        k: str(tmp_path / k)
        for k in (
            "project_dir",
            "source_dir",
            "ark_files_root",
            "config_root",
            "gpg_home",
            "output_root",
            "work_root",
        )
    }
    config.update(
        key_file=str(key),
        containers={k: {"id": "a" * 64} for k in ("postgres", "app", "controller")},
    )
    recovery.validate_config(config)
    key.chmod(0o644)
    with pytest.raises(recovery.RecoveryError, match="0600"):
        recovery.validate_config(config)
    key.chmod(0o600)
    config["work_root"] = config["ark_files_root"] + "/work"
    with pytest.raises(recovery.RecoveryError, match="disjoint"):
        recovery.validate_config(config)


@pytest.mark.parametrize(
    "key_location",
    [
        ("ark_files_root", "nested/key"),
        ("source_dir", "key"),
        ("project_dir", ".env"),
        ("project_dir", "docker-compose.ark.yml"),
        ("config_root", "entity-discovery.json"),
        ("config_root", "import-manifests/nested/key"),
    ],
)
def test_recovery_passphrase_cannot_be_archived(tmp_path, key_location):
    config = {
        k: str(tmp_path / k)
        for k in (
            "project_dir",
            "source_dir",
            "ark_files_root",
            "config_root",
            "gpg_home",
            "output_root",
            "work_root",
        )
    }
    root, suffix = key_location
    key = Path(config[root]) / suffix
    key.parent.mkdir(parents=True)
    key.write_text("synthetic-passphrase")
    key.chmod(0o600)
    config.update(
        key_file=str(key),
        containers={k: {"id": "a" * 64} for k in ("postgres", "app", "controller")},
    )
    with pytest.raises(recovery.RecoveryError, match="outside captured sources"):
        recovery.validate_config(config)


def test_documented_key_under_uncaptured_recovery_config_is_allowed(tmp_path):
    config = {
        k: str(tmp_path / k)
        for k in (
            "project_dir",
            "source_dir",
            "ark_files_root",
            "config_root",
            "gpg_home",
            "output_root",
            "work_root",
        )
    }
    key = Path(config["config_root"]) / "recovery/backup-passphrase"
    key.parent.mkdir(parents=True)
    key.write_text("synthetic-passphrase")
    key.chmod(0o600)
    config.update(
        key_file=str(key),
        containers={k: {"id": "a" * 64} for k in ("postgres", "app", "controller")},
    )
    recovery.validate_config(config)


def test_runtime_snapshot_preserves_private_overrides_without_printing(
    monkeypatch, capsys, tmp_path
):
    item = {
        "Id": "a" * 64,
        "Image": "sha256:" + "b" * 64,
        "State": {"Status": "running", "Health": {"Status": "healthy"}},
        "Config": {"Env": ["SESSION_SECRET_KEY=synthetic-private"], "Cmd": ["worker"]},
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        "Mounts": [{"Source": "/synthetic/source", "Destination": "/ark-files"}],
        "NetworkSettings": {"Networks": {"private-network": {"Aliases": ["worker"]}}},
    }
    monkeypatch.setattr(recovery, "command", lambda args: json.dumps([item]))
    config = {"containers": {"controller": {"name": "worker", "id": "a" * 64}}}
    snapshot = recovery.private_runtime_descriptors(config)
    private = tmp_path / "runtime.private.json"
    recovery.write_json(private, snapshot)
    assert json.loads(private.read_text()) == {"controller": item}
    assert private.stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr().out == ""
    contract = recovery.runtime_contract(snapshot)
    snapshot["controller"]["State"]["Health"]["Status"] = "starting"
    assert recovery.runtime_contract(snapshot) == contract
    snapshot["controller"]["Config"] = {"Env": ["SESSION_SECRET_KEY=changed"]}
    assert recovery.runtime_contract(snapshot) != contract
