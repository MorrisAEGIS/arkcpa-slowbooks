"""PDF page-1 rasterization without poppler (issue #116).

The native renderers only run on their OS; here their bridges are faked so
the seam (order, fallback, per-platform message, status reporting) is
tested everywhere, and the real poppler path runs where poppler is."""

import asyncio
import shutil

import pytest

from app.services import ocr_engines, ocr_service, pdf_raster

PNG = b"\x89PNG\r\n\x1a\nfake"


# ---------------------------------------------------------------------------
# Fake WinRT bridge — mirrors the objects _windows_render touches
# ---------------------------------------------------------------------------


class _Size:
    width = 612.0
    height = 792.0


class _Stream:
    def __init__(self):
        self.buf = bytearray()
        self.pos = 0

    def get_output_stream_at(self, _pos):
        return self

    def get_input_stream_at(self, _pos):
        return self

    def seek(self, pos):
        self.pos = pos

    @property
    def size(self):
        return len(self.buf)


class _Writer:
    def __init__(self, stream):
        self.stream = stream

    def write_bytes(self, data):
        self.stream.buf += data

    async def store_async(self):
        return len(self.stream.buf)

    async def flush_async(self):
        return True

    def detach_stream(self):
        return self.stream


class _Reader:
    def __init__(self, stream):
        self.stream = stream

    async def load_async(self, n):
        return n

    def read_buffer(self, n):
        return bytes(self.stream.buf[:n])


class _Page:
    size = _Size()

    def __init__(self, calls):
        self.calls = calls

    async def render_to_stream_async(self, out):  # the one-argument WinRT method
        raise AssertionError("render_to_stream_async takes no options")

    async def render_with_options_to_stream_async(self, out, opts):
        self.calls.append((opts.destination_width, opts.destination_height))
        out.buf += PNG


class _Doc:
    page_count = 3

    def __init__(self, calls):
        self.calls = calls

    def get_page(self, index):
        assert index == 0
        return _Page(self.calls)


def _fake_winrt(calls, loaded):
    class PdfDocument:
        @staticmethod
        async def load_from_stream_async(stream):
            loaded.append(bytes(stream.buf))
            return _Doc(calls)

    class PdfPageRenderOptions:
        destination_width = 0
        destination_height = 0

    return lambda: (PdfDocument, PdfPageRenderOptions, _Reader, _Writer, _Stream)


def test_windows_renderer_uses_winrt_at_300_dpi(monkeypatch):
    calls, loaded = [], []
    monkeypatch.setattr(pdf_raster, "_winrt_bridge", _fake_winrt(calls, loaded))
    monkeypatch.setattr(
        ocr_engines, "_run_winrt_coroutine", lambda factory: asyncio.run(factory())
    )
    png, pages = pdf_raster._windows_render(b"%PDF-1.4 fake", 300)
    assert png == PNG and pages == 3
    assert loaded == [b"%PDF-1.4 fake"]
    # Letter at 300 dpi: 612pt × 300/72 = 2550 px wide, 3300 tall
    assert calls == [(2550, 3300)]


def test_windows_available_only_on_win32(monkeypatch):
    monkeypatch.setattr(pdf_raster, "_winrt_bridge", _fake_winrt([], []))
    monkeypatch.setattr(pdf_raster.sys, "platform", "linux")
    assert pdf_raster.windows_available() is False
    monkeypatch.setattr(pdf_raster.sys, "platform", "win32")
    assert pdf_raster.windows_available() is True

    def boom():
        raise ImportError("no winrt")

    monkeypatch.setattr(pdf_raster, "_winrt_bridge", boom)
    assert pdf_raster.windows_available() is False


def test_macos_available_requires_the_imageio_functions(monkeypatch):
    """`import Quartz` working is not enough: the render also needs ImageIO's
    CGImageDestination*, which PyInstaller collects separately."""
    import sys as _sys
    import types

    quartz = types.ModuleType("Quartz")
    for name in pdf_raster._QUARTZ_NEEDED:
        setattr(quartz, name, lambda *a, **k: None)
    monkeypatch.setitem(_sys.modules, "Quartz", quartz)
    monkeypatch.setitem(_sys.modules, "Foundation", types.ModuleType("Foundation"))
    monkeypatch.setattr(pdf_raster.sys, "platform", "darwin")
    assert pdf_raster.macos_available() is True
    delattr(quartz, "CGImageDestinationCreateWithData")
    assert pdf_raster.macos_available() is False


# ---------------------------------------------------------------------------
# Order and fallback
# ---------------------------------------------------------------------------


def test_native_renderer_wins_over_poppler(monkeypatch):
    monkeypatch.setattr(pdf_raster, "windows_available", lambda: True)
    monkeypatch.setattr(pdf_raster, "_windows_render", lambda d, dpi: (PNG, 1))

    def never(*a):
        raise AssertionError("poppler must not run when a native renderer works")

    monkeypatch.setattr(pdf_raster, "_poppler_render", never)
    assert pdf_raster.rasterize(b"x", poppler_ok=True) == (PNG, 1)
    assert pdf_raster.pdf_renderer(poppler_ok=True) == "windows"


def test_native_failure_falls_back_to_poppler(monkeypatch):
    monkeypatch.setattr(pdf_raster, "macos_available", lambda: True)

    def broken(d, dpi):
        raise ValueError("Quartz said no")

    monkeypatch.setattr(pdf_raster, "_macos_render", broken)
    monkeypatch.setattr(pdf_raster, "_poppler_render", lambda d, dpi: (PNG, 2))
    assert pdf_raster.rasterize(b"x", poppler_ok=True) == (PNG, 2)


def test_library_error_text_never_reaches_the_user(monkeypatch):
    """A WinRT HRESULT or a Quartz message is not a ValueError of ours: log it,
    answer with our own words (skytech: '[WinError -2147188716] …' in a 400)."""
    monkeypatch.setattr(pdf_raster, "windows_available", lambda: True)

    def broken(d, dpi):
        raise OSError(
            -2147188716, "The text associated with this error code could not be found."
        )

    monkeypatch.setattr(pdf_raster, "_windows_render", broken)
    with pytest.raises(ValueError) as exc:
        pdf_raster.rasterize(b"x", poppler_ok=False)
    assert "WinError" not in str(exc.value) and "2147188716" not in str(exc.value)
    assert "valid, unencrypted" in str(exc.value)


def test_present_renderer_failing_on_this_file_is_a_file_error(monkeypatch):
    monkeypatch.setattr(pdf_raster, "windows_available", lambda: True)

    def broken(d, dpi):
        raise ValueError("The PDF has no pages")

    monkeypatch.setattr(pdf_raster, "_windows_render", broken)
    with pytest.raises(ValueError, match="no pages"):
        pdf_raster.rasterize(b"x", poppler_ok=False)


@pytest.mark.parametrize(
    "platform, needle",
    [
        ("win32", "Windows PDF renderer"),
        ("darwin", "brew install poppler"),
        ("linux", "sudo apt install poppler-utils"),
    ],
)
def test_message_names_the_fix_for_this_platform(monkeypatch, platform, needle):
    monkeypatch.setattr(pdf_raster, "windows_available", lambda: False)
    monkeypatch.setattr(pdf_raster, "macos_available", lambda: False)
    monkeypatch.setattr(pdf_raster.sys, "platform", platform)
    assert pdf_raster.pdf_renderer(poppler_ok=False) is None
    with pytest.raises(ValueError) as exc:
        pdf_raster.rasterize(b"x", poppler_ok=False)
    assert needle in str(exc.value)
    assert "image" in str(exc.value).lower()  # images still scan


# ---------------------------------------------------------------------------
# Real poppler path (where poppler is) and the status surface
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("pdftoppm") is None or shutil.which("pdfinfo") is None,
    reason="poppler-utils not installed",
)
def test_poppler_renders_a_real_pdf(monkeypatch):
    from weasyprint import HTML

    pdf = HTML(string="<h1>Receipt</h1><p>Total $12.34</p>").write_pdf()
    monkeypatch.setattr(pdf_raster, "windows_available", lambda: False)
    monkeypatch.setattr(pdf_raster, "macos_available", lambda: False)
    png, pages = pdf_raster.rasterize(pdf, dpi=72)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert pages == 1


def test_status_reports_the_pdf_renderer(client, monkeypatch):
    monkeypatch.setattr(pdf_raster, "windows_available", lambda: False)
    monkeypatch.setattr(pdf_raster, "macos_available", lambda: False)
    ocr_service._cache.update(at=0.0, info=None)
    r = client.get("/api/ocr/status")
    assert r.status_code == 200
    expected = "poppler" if pdf_raster.poppler_available() else None
    assert r.json()["pdf"] == expected
    ocr_service._cache.update(at=0.0, info=None)
