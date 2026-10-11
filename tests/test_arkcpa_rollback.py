import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "rollback", Path(__file__).resolve().parents[1] / "scripts/arkcpa_rollback.py"
)
rollback = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rollback)


def env_file(tmp_path, host="cpa.enerteqpower.com"):
    path = tmp_path / ".env"
    path.write_text(
        f"ARKCPA_PUBLIC_HOSTNAME={host}\nAUTHENTIK_OIDC_REDIRECT_URI=https://{host}/api/auth/authentik/callback\nSESSION_SECRET_KEY=synthetic-never-copy\n"
    )
    return path


def test_rollback_rejects_old_nonresolving_hostname(tmp_path):
    with pytest.raises(ValueError, match="canonical hostname"):
        rollback.canonical_environment(env_file(tmp_path, "arkcpa.magaenergy.ai"))


def test_rollback_preserves_canonical_environment_and_pins(tmp_path, monkeypatch):
    environment = env_file(tmp_path)
    before = environment.read_bytes()
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n")
    image = "sha256:" + "a" * 64
    candidate = "sha256:" + "b" * 64

    def inspect(target, image=False):
        if image:
            return {"Id": target}
        return {
            "Id": target + "-identity",
            "Image": candidate if target == "web" else "unchanged",
        }

    monkeypatch.setattr(rollback, "inspect", inspect)
    plan = rollback.prepare(
        compose,
        environment,
        tmp_path / "plan",
        image,
        candidate,
        {"postgres": "pg", "controller": "worker", "app": "web"},
    )
    assert rollback.check(plan)["activation_performed"] is False
    assert environment.read_bytes() == before
    assert "synthetic-never-copy" not in repr(plan)
    assert plan["activation_command"][-5:] == [
        "--no-deps",
        "--no-build",
        "--pull",
        "never",
        "slowbooks",
    ]
    assert "--no-deps" in plan["activation_command"]
    assert plan["identity_and_dns_mutation"] is False
    environment.write_text(environment.read_text() + "# concurrent change\n")
    with pytest.raises(ValueError, match="changed"):
        rollback.check(plan)


def test_rollback_rejects_callback_with_other_origin(tmp_path):
    path = env_file(tmp_path)
    path.write_text(
        path.read_text().replace(
            "https://cpa.enerteqpower.com", "https://untrusted.invalid"
        )
    )
    with pytest.raises(ValueError, match="callback"):
        rollback.canonical_environment(path)
