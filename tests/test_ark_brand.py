"""Ark CPA identity, login isolation, and Living Ark contract guards."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_static_shell_declares_ark_cpa_identity_and_icons():
    html = read("index.html")
    assert "<title>Ark CPA</title>" in html
    assert '<div class="topbar-brand">Ark CPA <span>Controller</span></div>' in html
    assert "<h1>Ark CPA</h1>" in html
    assert 'class="auth-pending"' in html
    assert 'id="auth-root"' in html
    assert 'href="/static/brand/ark-favicon-32.png"' in html
    assert 'href="/static/brand/ark-apple-touch-icon.png"' in html
    assert 'href="/static/manifest.webmanifest"' in html
    assert "viewport-fit=cover" in html


def test_login_is_a_dedicated_inert_application_state(unauthed_client):
    response = unauthed_client.get("/login")
    assert response.status_code == 200
    assert 'id="app" aria-hidden="true" inert' in response.text
    assert (
        'id="splash" class="splash-overlay" aria-hidden="true" inert' in response.text
    )
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
    assert manifest["start_url"] == "/"
    assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}


def test_auth_markup_uses_ark_classes_without_inline_presentation():
    source = read("app/static/js/auth.js")
    assert "MAGA Energy / Accounting" in source
    assert "Continue with Authentik" in source
    assert "Local recovery access" in source
    assert "Built on SlowBooks Pro 2026" in source
    assert 'window.location.pathname !== "/login"' in source
    assert "element.inert = true" in source
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


def test_brand_foundation_does_not_load_remote_fonts():
    combined = (
        read("index.html")
        + read("app/static/css/style.css")
        + read("app/static/css/ark-brand.css")
    )
    assert "fonts.googleapis.com" not in combined
    assert "fonts.gstatic.com" not in combined


def test_upstream_notice_retains_required_acknowledgement():
    notice = read("NOTICE")
    license_text = read("LICENSE")
    required = "Slowbooks Pro 2026 — originally created by Trent Von Holten"
    assert required in notice
    assert required in license_text
