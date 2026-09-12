import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

MACOS_DIR = Path(__file__).resolve().parent.parent / "packaging" / "macos"
sys.path.insert(0, str(MACOS_DIR))
SPEC = importlib.util.spec_from_file_location(
    "slowbooks_macos_release", MACOS_DIR / "release.py"
)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
sys.path.pop(0)


def test_parse_build_info_rejects_duplicate_keys():
    with pytest.raises(ValueError, match="invalid build metadata"):
        release._parse_key_values("git_sha=one\ngit_sha=two\n")


def test_developer_identities_only_returns_application_certificates():
    output = """
      1) ABCDEF1234 "Developer ID Application: Example Person (TEAM123456)"
      2) 1234ABCDEF "Apple Development: Example Person (TEAM123456)"
         2 valid identities found
    """

    assert release._developer_identities(output) == [
        "Developer ID Application: Example Person (TEAM123456)"
    ]


def test_verify_checksums_rejects_tampered_artifact(tmp_path):
    artifact = tmp_path / "artifact.zip"
    artifact.write_bytes(b"original")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (tmp_path / "SHA256SUMS").write_text(
        f"{digest}  {artifact.name}\n", encoding="utf-8"
    )
    release._verify_checksums(tmp_path)

    artifact.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum mismatch"):
        release._verify_checksums(tmp_path)


def test_sign_app_signs_nested_code_inside_out(monkeypatch, tmp_path):
    app = tmp_path / "SlowBooks Pro.app"
    executable = app / "Contents" / "MacOS" / "SlowBooksPro"
    dylib = app / "Contents" / "Frameworks" / "libexample.dylib"
    plugin = app / "Contents" / "PlugIns" / "Example.bundle"
    plugin_binary = plugin / "Contents" / "MacOS" / "Example"
    for path in (executable, dylib, plugin_binary):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mach-o")

    classifications = {
        executable: (True, True),
        dylib: (True, False),
        plugin_binary: (True, True),
    }
    signed = []
    monkeypatch.setattr(
        release,
        "_is_macho",
        lambda path: classifications.get(path, (False, False)),
    )
    monkeypatch.setattr(
        release,
        "_sign",
        lambda path, identity, hardened_runtime: signed.append(
            (path, identity, hardened_runtime)
        ),
    )

    release._sign_app(app, "Developer ID Application: Example (TEAM123456)")

    signed_paths = [path for path, _, _ in signed]
    assert signed_paths.index(plugin_binary) < signed_paths.index(plugin)
    assert signed_paths.index(plugin) < signed_paths.index(app)
    assert signed_paths[-1] == app
    assert (dylib, "Developer ID Application: Example (TEAM123456)", False) in signed
    assert (
        executable,
        "Developer ID Application: Example (TEAM123456)",
        True,
    ) in signed


def test_signature_details_require_matching_team_timestamp_and_runtime():
    identity = "Developer ID Application: Example (TEAM123456)"
    details = (
        f"Authority={identity}\n"
        "TeamIdentifier=TEAM123456\n"
        "Timestamp=Aug 12, 2026 at 12:00:00\n"
        "flags=0x10000(runtime)\n"
    )

    assert (
        release._verify_signature_details(
            details,
            identity,
            expected_team_id="TEAM123456",
            hardened_runtime=True,
        )
        == "TEAM123456"
    )

    with pytest.raises(RuntimeError, match="hardened runtime"):
        release._verify_signature_details(
            details.replace("flags=0x10000(runtime)\n", ""),
            identity,
            expected_team_id="TEAM123456",
            hardened_runtime=True,
        )


def test_pre_notary_policy_allows_only_lone_unnotarized_rejection():
    syspolicy = subprocess.CompletedProcess(
        ("syspolicy_check",),
        70,
        stdout=("Codesign Error\nSeverity: Fatal\n" "Gatekeeper rejected this file.\n"),
        stderr="",
    )
    gatekeeper = subprocess.CompletedProcess(
        ("spctl",),
        3,
        stdout="",
        stderr="rejected\nsource=Unnotarized Developer ID\n",
    )

    assert release._expected_unnotarized_policy_result(syspolicy, gatekeeper)

    with_structure_error = subprocess.CompletedProcess(
        syspolicy.args,
        syspolicy.returncode,
        stdout=syspolicy.stdout + "Incorrect Bundle Structure\nSeverity: Warning\n",
        stderr="",
    )
    assert not release._expected_unnotarized_policy_result(
        with_structure_error, gatekeeper
    )


@pytest.mark.parametrize("issues", [[], None])
def test_notarize_inspects_accepted_log_and_staples(monkeypatch, tmp_path, issues):
    dmg = tmp_path / "SlowBooksPro.dmg"
    dmg.write_bytes(b"dmg")
    submission_id = "11111111-2222-3333-4444-555555555555"
    calls = []

    def fake_run(*args, check=True):
        calls.append(args)
        if args[1:3] == ("notarytool", "submit"):
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps({"id": submission_id, "status": "Accepted"}),
                stderr="",
            )
        if args[1:3] == ("notarytool", "log"):
            Path(args[4]).write_text(
                json.dumps(
                    {"jobId": submission_id, "status": "Accepted", "issues": issues}
                ),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(release, "_run", fake_run)

    release._notarize(dmg, "slowbooks-notary", tmp_path, "dmg")
    release._staple(dmg, tmp_path)

    assert (tmp_path / "notary-dmg-log.json").is_file()
    assert any(args[1:3] == ("stapler", "staple") for args in calls)
    assert any(args[1:3] == ("stapler", "validate") for args in calls)


def test_staple_fails_when_validate_fails(monkeypatch, tmp_path):
    app = tmp_path / "SlowBooks Pro.app"
    app.mkdir()

    def fake_run(*args, check=True):
        rc = 65 if args[1:3] == ("stapler", "validate") else 0
        return subprocess.CompletedProcess(args, rc, stdout="", stderr="no ticket")

    monkeypatch.setattr(release, "_run", fake_run)
    with pytest.raises(RuntimeError, match="no notarization ticket"):
        release._staple(app, tmp_path)


def test_notarize_rejects_log_with_errors(monkeypatch, tmp_path):
    dmg = tmp_path / "SlowBooksPro.dmg"
    dmg.write_bytes(b"dmg")
    submission_id = "11111111-2222-3333-4444-555555555555"

    def fake_run(*args, check=True):
        if args[1:3] == ("notarytool", "submit"):
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps({"id": submission_id, "status": "Accepted"}),
                stderr="",
            )
        if args[1:3] == ("notarytool", "log"):
            Path(args[4]).write_text(
                json.dumps(
                    {
                        "jobId": submission_id,
                        "status": "Accepted",
                        "issues": [{"severity": "error", "message": "bad code"}],
                    }
                ),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(release, "_run", fake_run)

    with pytest.raises(RuntimeError, match="did not pass inspection"):
        release._notarize(dmg, "slowbooks-notary", tmp_path, "dmg")


def test_dmg_contents_check_uses_the_staged_bundle_name(monkeypatch, tmp_path):
    """#100: the mounted-DMG stapler check must look for the bundle by the
    name that was actually staged, not a hardcoded 'SlowBooks Pro.app'."""
    calls = []

    def fake_record_run(report_path, *args, check=True):
        calls.append(args)
        if args[:2] == ("hdiutil", "attach"):
            (tmp_path / "final-dmg-mount" / "Renamed.app").mkdir(parents=True)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(release, "_record_run", fake_record_run)
    dmg = tmp_path / "SlowBooksPro.dmg"
    dmg.write_bytes(b"dmg")
    release._verify_dmg_contents_stapled(
        dmg, tmp_path / "ev.txt", tmp_path, "Renamed.app"
    )
    validated = [c for c in calls if c[1:3] == ("stapler", "validate")]
    assert validated and validated[0][-1].endswith("/final-dmg-mount/Renamed.app")
    assert calls[-1][:2] == ("hdiutil", "detach")


def test_dmg_contents_check_fails_when_bundle_is_missing(monkeypatch, tmp_path):
    calls = []

    def fake_record_run(report_path, *args, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(release, "_record_run", fake_record_run)
    dmg = tmp_path / "SlowBooksPro.dmg"
    dmg.write_bytes(b"dmg")
    with pytest.raises(RuntimeError, match="is not inside"):
        release._verify_dmg_contents_stapled(
            dmg, tmp_path / "ev.txt", tmp_path, "Missing.app"
        )
    assert calls[-1][:2] == ("hdiutil", "detach")


# --- testing-repo #28: retry Apple's timestamp service, nothing else ---------


class _Fail(Exception):
    pass


def _cpe(stderr):
    import subprocess

    return subprocess.CalledProcessError(1, ("codesign",), output="", stderr=stderr)


def test_timestamp_failure_is_retried_then_succeeds(monkeypatch):
    calls = []
    slept = []
    answers = [
        _cpe("x.dmg: A timestamp was expected but was not found.\n"),
        _cpe("x.dmg: A timestamp was expected but was not found.\n"),
        "ok",
    ]

    def fake_run(*args):
        calls.append(args)
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(release, "_run", fake_run)
    monkeypatch.setattr(release, "_sleep", lambda s: slept.append(s))
    assert release._run_signing("codesign", "--timestamp", "x.dmg") == "ok"
    assert len(calls) == 3
    assert slept == [30, 60]


def test_other_signing_failures_surface_on_the_first_try(monkeypatch):
    import subprocess

    calls = []

    def fake_run(*args):
        calls.append(args)
        raise _cpe("x.dmg: code object is not signed at all\n")

    monkeypatch.setattr(release, "_run", fake_run)
    monkeypatch.setattr(
        release, "_sleep", lambda s: (_ for _ in ()).throw(AssertionError("no sleep"))
    )
    with pytest.raises(subprocess.CalledProcessError):
        release._run_signing("codesign", "--timestamp", "x.dmg")
    assert len(calls) == 1


def test_timestamp_failure_gives_up_after_three(monkeypatch):
    import subprocess

    calls = []

    def fake_run(*args):
        calls.append(args)
        raise _cpe("A timestamp was expected but was not found")

    monkeypatch.setattr(release, "_run", fake_run)
    monkeypatch.setattr(release, "_sleep", lambda s: None)
    with pytest.raises(subprocess.CalledProcessError):
        release._run_signing("codesign", "--timestamp", "x.dmg")
    assert len(calls) == 3


def test_sign_and_dmg_codesign_go_through_the_retry():
    src = (MACOS_DIR / "release.py").read_text(encoding="utf-8")
    body = src.split("def _sign(")[1].split("def _sign_app(")[0]
    assert "_run_signing(*command)" in body
    dmg = src.split('f"{BUNDLE_ID}.dmg"')[0]
    assert dmg.rstrip().endswith('"--identifier",') and "_run_signing(" in dmg[-400:]
