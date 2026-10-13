from hashlib import sha256
from pathlib import Path
import re

from app.services.asset_versions import versioned_shell


def test_release_shell_binds_every_local_script_and_stylesheet_to_bytes(client):
    root = Path(__file__).parents[1]
    for route in ("/", "/login"):
        response = client.get(route)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assets = re.findall(
            r'(?:src|href)=["\'](/static/[^"\']+\.(?:js|css))\?v=([a-f0-9]+)',
            response.text,
        )
        assert len(assets) > 50
        for path, revision in assets:
            assert (
                revision
                == sha256((root / "app" / path.lstrip("/")).read_bytes()).hexdigest()[
                    :24
                ]
            )
        assert not re.search(
            r'(?:src|href)=["\']/static/[^"\']+\.(?:js|css)["\']', response.text
        )


def test_changed_asset_changes_cache_identity_after_process_restart(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    asset = static / "app.js"
    asset.write_text("old-release")
    index = tmp_path / "index.html"
    index.write_text(
        '<script src="/static/app.js"></script><script src="https://example.test/external.js"></script>'
    )
    old = versioned_shell(index, static)
    asset.write_text("new-release")
    versioned_shell.cache_clear()
    new = versioned_shell(index, static)
    assert old != new
    assert 'src="https://example.test/external.js"' in new
