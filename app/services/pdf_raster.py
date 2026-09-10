"""Rasterize page 1 of a PDF for OCR — natively where the OS can (issue #116).

Scanning an image needs no install on Windows or macOS (the OS engines do
the reading); a PDF first has to become an image, and until 2.10 only
poppler's pdftoppm did that, which the desktop installers do not ship.
Now:

  Windows  → Windows.Data.Pdf through the same WinRT projection the OCR
             engine uses (frozen build; nothing to install)
  macOS    → Quartz (CGPDFDocument), already in the bundle for Vision
  anywhere → poppler-utils (pdftoppm/pdfinfo) when on PATH — the Linux
             path, and the fallback on the desktops

Each renderer returns (png_bytes, page_count) or raises; the first one
that works wins, and the message when none does names the fix for THIS
platform, not a Linux command.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PDF_DPI = 300

logger = logging.getLogger(__name__)


class PdfRasterUnavailable(ValueError):
    """No renderer could rasterize the PDF on this machine."""


# ---------------------------------------------------------------------------
# poppler (pdftoppm / pdfinfo)
# ---------------------------------------------------------------------------


def poppler_available() -> bool:
    return shutil.which("pdftoppm") is not None and shutil.which("pdfinfo") is not None


def _poppler_render(data: bytes, dpi: int) -> tuple[bytes, int]:
    with tempfile.TemporaryDirectory(prefix="slowbooks-ocr-") as tmp:
        pdf_path = Path(tmp) / "input.pdf"
        pdf_path.write_bytes(data)
        page_count = 1
        try:
            info = subprocess.run(
                ["pdfinfo", str(pdf_path)], capture_output=True, text=True, timeout=15
            )
            if info.returncode == 0:
                m = re.search(r"^Pages:\s*(\d+)", info.stdout, re.MULTILINE)
                if m:
                    page_count = int(m.group(1))
        except (OSError, subprocess.TimeoutExpired):
            pass
        prefix = Path(tmp) / "page"
        try:
            proc = subprocess.run(
                [
                    "pdftoppm",
                    "-png",
                    "-r",
                    str(dpi),
                    "-f",
                    "1",
                    "-l",
                    "1",
                    "-singlefile",
                    str(pdf_path),
                    str(prefix),
                ],
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"pdftoppm could not run: {exc}") from exc
        if proc.returncode != 0:
            raise ValueError(
                "pdftoppm could not read the PDF — is it a valid, unencrypted file?"
            )
        out = Path(tmp) / "page.png"
        if not out.exists():
            raise ValueError("pdftoppm produced no image")
        return out.read_bytes(), page_count


# ---------------------------------------------------------------------------
# Windows — Windows.Data.Pdf via the winrt projection (frozen build)
# ---------------------------------------------------------------------------


def _winrt_bridge():
    try:
        from winrt.windows.data.pdf import PdfDocument, PdfPageRenderOptions
        from winrt.windows.storage.streams import (
            DataReader,
            DataWriter,
            InMemoryRandomAccessStream,
        )
    except ImportError:
        from winsdk.windows.data.pdf import PdfDocument, PdfPageRenderOptions
        from winsdk.windows.storage.streams import (
            DataReader,
            DataWriter,
            InMemoryRandomAccessStream,
        )
    return (
        PdfDocument,
        PdfPageRenderOptions,
        DataReader,
        DataWriter,
        InMemoryRandomAccessStream,
    )


def windows_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        _winrt_bridge()
        return True
    except Exception:
        return False


def _windows_render(data: bytes, dpi: int) -> tuple[bytes, int]:
    from app.services.ocr_engines import _run_winrt_coroutine

    PdfDocument, PdfPageRenderOptions, DataReader, DataWriter, Stream = _winrt_bridge()

    async def _run():
        src = Stream()
        writer = DataWriter(src.get_output_stream_at(0))
        writer.write_bytes(data)
        await writer.store_async()
        await writer.flush_async()
        writer.detach_stream()
        src.seek(0)
        doc = await PdfDocument.load_from_stream_async(src)
        page_count = int(doc.page_count)
        if page_count < 1:
            raise ValueError("The PDF has no pages")
        page = doc.get_page(0)
        opts = PdfPageRenderOptions()
        opts.destination_width = max(1, int(page.size.width * dpi / 72.0))
        opts.destination_height = max(1, int(page.size.height * dpi / 72.0))
        out = Stream()
        # RenderToStreamAsync takes the stream alone; the overload that
        # takes options is a separate method in WinRT (skytech, 2.10.0
        # gate: "Invalid parameter count" on every PDF).
        await page.render_with_options_to_stream_async(out, opts)
        out.seek(0)
        size = int(out.size)
        reader = DataReader(out.get_input_stream_at(0))
        await reader.load_async(size)
        buf = reader.read_buffer(size)
        return bytes(buf), page_count

    return _run_winrt_coroutine(_run)


# ---------------------------------------------------------------------------
# macOS — Quartz CGPDFDocument (pyobjc-framework-Quartz, in the bundle)
# ---------------------------------------------------------------------------


_QUARTZ_NEEDED = (
    "CGDataProviderCreateWithCFData",
    "CGPDFDocumentCreateWithProvider",
    "CGPDFDocumentGetNumberOfPages",
    "CGPDFDocumentGetPage",
    "CGPDFPageGetBoxRect",
    "CGBitmapContextCreate",
    "CGContextDrawPDFPage",
    "CGBitmapContextCreateImage",
    "CGImageDestinationCreateWithData",  # ImageIO — a separately collected submodule
    "CGImageDestinationAddImage",
    "CGImageDestinationFinalize",
)


def _quartz_bridge():
    """Quartz and Foundation, with every function the render uses proven
    present — `import Quartz` succeeding says nothing about ImageIO, which
    PyInstaller collects separately, and the status field the SPA shows
    must not promise a renderer that then fails (macbase1, 2.10.0 gate)."""
    import Foundation
    import Quartz

    missing = [name for name in _QUARTZ_NEEDED if not hasattr(Quartz, name)]
    if missing:
        raise ImportError(f"Quartz lacks {', '.join(missing)}")
    return Quartz, Foundation


def macos_available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        _quartz_bridge()
        return True
    except Exception:
        return False


def _macos_render(data: bytes, dpi: int) -> tuple[bytes, int]:
    Quartz, Foundation = _quartz_bridge()
    provider = Quartz.CGDataProviderCreateWithCFData(
        Foundation.NSData.dataWithBytes_length_(data, len(data))
    )
    doc = Quartz.CGPDFDocumentCreateWithProvider(provider)
    if doc is None:
        raise ValueError("Could not read the PDF — is it a valid, unencrypted file?")
    page_count = int(Quartz.CGPDFDocumentGetNumberOfPages(doc))
    if page_count < 1:
        raise ValueError("The PDF has no pages")
    page = Quartz.CGPDFDocumentGetPage(doc, 1)
    box = Quartz.CGPDFPageGetBoxRect(page, Quartz.kCGPDFMediaBox)
    scale = dpi / 72.0
    width = max(1, int(box.size.width * scale))
    height = max(1, int(box.size.height * scale))
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(
        None, width, height, 8, 0, cs, Quartz.kCGImageAlphaNoneSkipLast
    )
    if ctx is None:
        raise ValueError("Could not create a drawing context for the PDF")
    Quartz.CGContextSetRGBFillColor(ctx, 1, 1, 1, 1)
    Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, width, height))
    Quartz.CGContextScaleCTM(ctx, scale, scale)
    Quartz.CGContextTranslateCTM(ctx, -box.origin.x, -box.origin.y)
    Quartz.CGContextDrawPDFPage(ctx, page)
    image = Quartz.CGBitmapContextCreateImage(ctx)
    out = Foundation.NSMutableData.data()
    dest = Quartz.CGImageDestinationCreateWithData(out, "public.png", 1, None)
    if dest is None:
        raise ValueError("Could not encode the rendered page")
    Quartz.CGImageDestinationAddImage(dest, image, None)
    if not Quartz.CGImageDestinationFinalize(dest):
        raise ValueError("Could not encode the rendered page")
    return bytes(out), page_count


# ---------------------------------------------------------------------------
# The chokepoint
# ---------------------------------------------------------------------------


def renderers(poppler_ok: bool | None = None) -> list:
    """(name, available(), render(data, dpi)) in preference order.
    ``poppler_ok`` overrides the PATH probe (ocr_service caches it)."""
    poppler = poppler_available if poppler_ok is None else (lambda: poppler_ok)
    return [
        ("windows", windows_available, _windows_render),
        ("macos", macos_available, _macos_render),
        ("poppler", poppler, _poppler_render),
    ]


def pdf_renderer(poppler_ok: bool | None = None) -> str | None:
    """Which renderer this machine would use, or None."""
    for name, avail, _ in renderers(poppler_ok):
        try:
            if avail():
                return name
        except Exception:
            continue
    return None


def unavailable_message() -> str:
    if sys.platform == "win32":
        return (
            "PDF scanning needs the Windows PDF renderer, which this build could "
            "not load. Scan an image instead, or install poppler for Windows "
            "(winget install oschwartz10612.Poppler) and add its bin folder to PATH."
        )
    if sys.platform == "darwin":
        return (
            "PDF scanning needs the macOS PDF renderer, which this build could not "
            "load. Scan an image instead, or run: brew install poppler"
        )
    return (
        "PDF scanning requires poppler-utils (pdftoppm/pdfinfo). "
        "Install it to scan PDFs — images still work without it. "
        "Ubuntu/Debian: sudo apt install poppler-utils"
    )


def rasterize(
    data: bytes, dpi: int = PDF_DPI, poppler_ok: bool | None = None
) -> tuple[bytes, int]:
    """PNG bytes of page 1 and the page count, from the first renderer that
    works here. A renderer that is present but fails on this file hands
    over to the next; when none is present the error names the fix."""
    last_error: Exception | None = None
    tried = False
    for name, avail, render in renderers(poppler_ok):
        try:
            if not avail():
                continue
        except Exception:
            continue
        tried = True
        try:
            return render(data, dpi)
        except Exception as exc:  # noqa: BLE001 — try the next renderer
            last_error = exc
            continue
    if not tried:
        raise PdfRasterUnavailable(unavailable_message())
    # Our own renderers speak in ValueError with our own words; anything
    # else is a library's text (a WinRT HRESULT, a Quartz message) and
    # stays in the log — never in the response (skytech, 2.10.0 gate).
    if isinstance(last_error, ValueError):
        raise ValueError(str(last_error))
    logger.warning("PDF rasterization failed: %r", last_error)
    raise ValueError("Could not read the PDF — is it a valid, unencrypted file?")
