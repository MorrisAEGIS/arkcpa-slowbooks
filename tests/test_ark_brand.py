"""Ark CPA identity, login isolation, and Living Ark contract guards."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path

from app import __version__


ROOT = Path(__file__).resolve().parent.parent


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_static_shell_declares_ark_cpa_identity_and_icons():
    html = read("index.html")
    assert '<html lang="en" data-theme="dark" data-product="Ark CPA">' in html
    assert "<title>Ark CPA — MAGA Energy accounting</title>" in html
    assert '<strong>Ark CPA</strong>' in html
    assert '<h1>Ark CPA</h1>' in html
    assert html.count("MAGA Energy accounting") >= 3
    assert 'class="auth-pending"' in html
    assert 'id="auth-root"' in html
    assert f'href="/static/brand/ark-favicon-32.png?v={__version__}"' in html
    assert f'href="/static/brand/ark-apple-touch-icon.png?v={__version__}"' in html
    assert f'href="/static/manifest.webmanifest?v={__version__}"' in html
    assert 'viewport-fit=cover' in html


def test_login_is_a_dedicated_inert_application_state(unauthed_client):
    response = unauthed_client.get("/login")
    assert response.status_code == 200
    assert 'id="app" aria-hidden="true" inert' in response.text
    assert 'id="splash" class="splash-overlay hidden"' in response.text
    assert 'aria-hidden="true" inert' in response.text
    assert "font-src 'self' data:" in response.headers["content-security-policy"]


def test_ark_assets_and_manifest_are_self_hosted(unauthed_client):
    for path in (
        "/favicon.ico",
        "/static/brand/ark-living-mark.png",
        "/static/brand/ark-favicon-32.png",
        "/static/brand/ark-apple-touch-icon.png",
        "/static/brand/ark-icon-192.png",
        "/static/brand/ark-icon-512.png",
        "/static/fonts/Inter-Regular.woff2",
        "/static/fonts/Inter-Medium.woff2",
        "/static/fonts/Inter-SemiBold.woff2",
    ):
        response = unauthed_client.get(path)
        assert response.status_code == 200, path

    manifest = json.loads(read("app/static/manifest.webmanifest"))
    assert manifest["name"] == "Ark CPA"
    assert manifest["short_name"] == "Ark CPA"
    assert manifest["description"] == "Private MAGA Energy accounting."
    assert manifest["start_url"] == "/"
    assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}


def test_auth_markup_uses_ark_classes_without_inline_presentation():
    source = read("app/static/js/auth.js")
    assert "MAGA Energy accounting" in source
    assert "Continue with Authentik" in source
    assert "Local recovery access" in source
    assert "MAGA Energy · Shared accounting workspace" in source
    assert "Powered by SlowBooks" not in source
    assert "Set up Ark CPA" in source
    assert 'window.location.pathname !== "/login"' in source
    assert 'element.inert = true' in source
    assert 'style="' not in source
    assert "primaryButtonStyle" not in source


def test_living_ark_motion_and_reduced_motion_contract():
    css = read("app/static/css/ark-brand.css")
    assert "ark-orbit-spin 2.5s" in css
    assert "ark-float 3s" in css
    assert "ark-aura-pulse 2s" in css
    assert "ark-water-rise 18s" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    reduced = css.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert "animation: none" in reduced
    auth_css = read("app/static/css/ark-auth.css")
    assert ".ark-auth-page .ark-mark" in auth_css
    assert "--ark-mark-size: 128px" in auth_css
    assert "--ark-mark-size: 108px" in auth_css


def test_brand_foundation_does_not_load_remote_fonts():
    combined = read("index.html") + read("app/static/css/style.css") + read(
        "app/static/css/ark-brand.css"
    )
    assert "fonts.googleapis.com" not in combined
    assert "fonts.gstatic.com" not in combined


def test_upstream_notice_retains_required_acknowledgement():
    notice = read("NOTICE")
    license_text = read("LICENSE")
    required = "Slowbooks Pro 2026 — originally created by Trent Von Holten"
    assert required in notice
    assert required in license_text


def test_canonical_ark_artwork_hash_is_pinned():
    icon = ROOT / "app/static/brand/ark-icon-512.png"
    living = ROOT / "app/static/brand/ark-living-mark.png"
    expected = "320ab26fcca091424e056e50b109def38acfb606d99dd98e079998fbdc4e0ff6"
    assert sha256(icon.read_bytes()).hexdigest() == expected
    assert sha256(living.read_bytes()).hexdigest() == expected


def test_all_top_level_assets_are_release_versioned():
    html = read("index.html")
    refs = re.findall(r'(?:href|src)="(/static/[^"]+)"', html)
    assert refs
    assert all(ref.endswith(f"?v={__version__}") for ref in refs), refs
    auth = read("app/static/js/auth.js")
    assert f"/static/brand/ark-living-mark.png?v={__version__}" in auth


def test_full_surface_visible_brand_contract():
    html = read("index.html")
    assert html.count("Slowbooks Pro 2026") == 1  # required About/legal acknowledgment
    assert "Powered by SlowBooks" not in html
    assert "ARK CPA" not in html
    assert "ArkCPA" not in html

    visible_templates = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "app/templates").rglob("*.html"))
    )
    assert "Slowbooks Pro" not in visible_templates
    assert "SlowBooks Pro" not in visible_templates
    assert "Powered by Slow" not in visible_templates
    assert "ArkCPA" not in visible_templates
    assert "ARK CPA" not in visible_templates
    assert visible_templates.count("Ark CPA") >= 14

    browser_copy = "\n".join(
        read(path)
        for path in (
            "app/static/js/api.js",
            "app/static/js/auth.js",
            "app/static/js/banking.js",
            "app/static/js/bootstrap.js",
            "app/static/js/companies.js",
            "app/static/js/employees.js",
            "app/static/js/iif.js",
            "app/static/js/ocr.js",
            "app/static/js/ocr_canvas.js",
            "app/static/js/qbo.js",
            "app/static/js/settings.js",
        )
    )
    assert "SlowBooks Pro" not in browser_copy
    assert "Slowbooks Pro" not in browser_copy
    assert "Powered by Slow" not in browser_copy


def test_empty_check_register_does_not_request_a_non_numeric_account():
    source = read("app/static/js/check_register.js")
    assert '<option value="" selected disabled>No bank accounts</option>' in source


def test_ark_application_shell_is_dark_first_light_capable_and_responsive():
    html = read("index.html")
    css = read("app/static/css/ark-brand.css")
    app_js = read("app/static/js/app.js")
    assert 'id="nav-toggle"' in html
    assert 'id="nav-backdrop"' in html
    assert 'aria-label="Accounting navigation"' in html
    assert ':root[data-theme="dark"]' in css
    assert ':root[data-theme="light"]' in css
    assert "@media (max-width: 960px)" in css
    assert "body.nav-open #sidebar" in css
    assert "ark-cpa-theme" in app_js
    assert "localStorage.getItem('slowbooks-theme')" in app_js
    assert "data-ark-release" in app_js


def test_api_and_generated_output_branding():
    assert 'title="Ark CPA API"' in read("app/main.py")
    assert 'from_name = smtp.get("smtp_from_name", "Ark CPA")' in read(
        "app/services/email_service.py"
    )
    assert '"# Ark CPA — Analytics Snapshot"' in read("app/routes/analytics.py")
    assert '"ark-cpa-export.iif"' in read("app/routes/iif.py")


def test_runtime_mountpoints_and_startup_logs_use_ark_cpa_contract():
    dockerfile = read("Dockerfile")
    entrypoint = read("docker-entrypoint.sh")
    assert "mkdir -p /app/backups /app/app/static/uploads" in dockerfile
    assert "chown -R slowbooks:slowbooks /app" in dockerfile
    assert 'echo "Ark CPA — Starting up..."' in entrypoint
    assert 'echo "Starting Ark CPA on port ${APP_PORT:-3001}..."' in entrypoint
    assert "Starting Slowbooks Pro" not in entrypoint
