from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_docker_build_context_excludes_private_runtime_data() -> None:
    patterns = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        ".env",
        ".slowbooks-master.key",
        ".slowbooks-session.key",
        "*.db",
        "*.sqlite3",
        "app/static/uploads/",
        "backups/",
    } <= patterns
