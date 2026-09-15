"""Authentication boundary for files stored below ``/static/uploads``."""


def test_login_assets_remain_public(unauthed_client):
    response = unauthed_client.get("/static/js/app.js")
    assert response.status_code == 200


def test_private_upload_namespaces_require_auth(unauthed_client):
    for path in (
        "/static/uploads",
        "/static/uploads/",
        "/static/uploads/intake/receipt.pdf",
        "/static/uploads/attachments/invoice/1/invoice.pdf",
    ):
        response = unauthed_client.get(path)
        assert response.status_code == 401, path


def test_dot_segments_cannot_walk_public_static_into_private_uploads():
    from app.main import _is_auth_exempt_path

    assert not _is_auth_exempt_path("/static/css/../uploads/intake/private-receipt.pdf")
    assert _is_auth_exempt_path("/portal/")


def test_fixed_raster_logo_name_is_public(unauthed_client):
    # A missing public asset reaches StaticFiles and returns 404; it must not be
    # intercepted by the session gate with 401.
    response = unauthed_client.get("/static/uploads/company_logo.png")
    assert response.status_code == 404


def test_svg_logo_name_is_not_public(unauthed_client):
    response = unauthed_client.get("/static/uploads/company_logo.svg")
    assert response.status_code == 401


def test_authenticated_session_can_reach_private_upload_namespace(client):
    response = client.get("/static/uploads/intake/not-present.pdf")
    assert response.status_code == 404
