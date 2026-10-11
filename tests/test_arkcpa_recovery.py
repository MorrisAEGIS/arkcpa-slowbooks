"""Recovery boundaries: unsafe content, inventory changes and false receipts."""

import importlib.util
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
