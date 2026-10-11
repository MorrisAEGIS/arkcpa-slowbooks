"""Exercise Compose interpolation with synthetic inputs, never the live .env."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which("docker"), reason="Docker Compose required")
def test_web_release_does_not_replace_controller_image(tmp_path):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    synthetic = tmp_path / "synthetic.env"
    values = {
        k: "synthetic"
        for k in (
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "POSTGRES_DB",
            "PAYROLL_ENCRYPTION_SECRET",
            "SESSION_SECRET_KEY",
            "SETTINGS_ENCRYPTION_KEY",
        )
    }
    values.update(
        APP_PUBLISH_PORT="13333",
        ARKCPA_WEB_IMAGE="web:candidate",
        ARKCPA_IMAGE="legacy:web",
        ARKCPA_CONTROLLER_IMAGE="sha256:" + "c" * 64,
        CORS_ALLOW_ORIGINS="https://cpa.enerteqpower.com",
    )
    synthetic.write_text("\n".join(f"{k}={v}" for k, v in values.items()))
    args = [
        "docker",
        "compose",
        "--env-file",
        str(synthetic),
        "-f",
        str(ROOT / "docker-compose.ark.yml"),
        "--profile",
        "controller",
        "config",
        "--format",
        "json",
    ]
    result = subprocess.run(args, capture_output=True, text=True, env=env, check=True)
    services = json.loads(result.stdout)["services"]
    assert services["slowbooks"]["image"] == "web:candidate"
    assert services["controller-worker"]["image"] == "sha256:" + "c" * 64
    assert "build" not in services["controller-worker"]
    synthetic.write_text(
        synthetic.read_text().replace("ARKCPA_CONTROLLER_IMAGE=sha256:" + "c" * 64, "")
    )
    assert subprocess.run(args, capture_output=True, env=env).returncode != 0
