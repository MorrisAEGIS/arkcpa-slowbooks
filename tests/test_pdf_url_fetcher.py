"""The PDF renderer fetches data: URIs only — file:// and http(s) are refused
on every WeasyPrint channel (CVE-2026-55073 made write_pdf() honour the
fetcher for stylesheets and metadata too; the pin moved to 70.0)."""

import pytest

from app.services import pdf_service


def test_data_uri_is_fetched():
    png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAD0lEQVR4nGP4z8DwHwYBAB0lB/ZKZ2fGAAAAAElFTkSuQmCC"
    resp = pdf_service._safe_url_fetcher(png)
    assert resp.headers.get_content_type() == "image/png"
    assert resp.read().startswith(b"\x89PNG")
    resp.close()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1:5432/",
        "https://example.com/x.css",
        "FILE:///etc/hostname",
    ],
)
def test_other_schemes_are_refused(url):
    with pytest.raises(ValueError, match="not allowed"):
        pdf_service._safe_url_fetcher(url)


def test_rendered_pdf_never_embeds_a_local_file(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("SLOWBOOKS-SECRET-MARKER-9f3a")
    html = (
        f'<h1>x</h1><img src="file://{secret}">'
        f'<link rel="stylesheet" href="file://{secret}">'
        f'<style>@import url("file://{secret}");</style>'
    )
    pdf = pdf_service.render_pdf(html)
    assert pdf.startswith(b"%PDF-")
    assert b"SLOWBOOKS-SECRET-MARKER-9f3a" not in pdf
