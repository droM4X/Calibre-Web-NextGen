# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for the embedded-cover extraction service."""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import types
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_extract_module():
    """Idempotently top up the cps stub so this test plays nicely with
    other service tests that import the same parent package."""
    cps_pkg = sys.modules.get("cps")
    if cps_pkg is None:
        cps_pkg = types.ModuleType("cps")
        cps_pkg.__path__ = [str(REPO_ROOT / "cps")]
        sys.modules["cps"] = cps_pkg

    constants = sys.modules.get("cps.constants") or types.ModuleType("cps.constants")
    if not hasattr(constants, "USER_AGENT"):
        constants.USER_AGENT = "Calibre-Web-NextGen-tests"
    sys.modules["cps.constants"] = constants
    cps_pkg.constants = constants

    logger_mod = sys.modules.get("cps.logger") or types.ModuleType("cps.logger")
    if not hasattr(logger_mod, "create"):
        logger_mod.create = lambda *_a, **_k: types.SimpleNamespace(
            debug=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
            info=lambda *_args, **_kwargs: None,
            error=lambda *_args, **_kwargs: None,
        )
    sys.modules["cps.logger"] = logger_mod
    cps_pkg.logger = logger_mod

    config_mod = sys.modules.get("cps.config") or types.ModuleType("cps.config")
    if not hasattr(config_mod, "get_book_path"):
        config_mod.get_book_path = lambda: "/tmp/library"
    sys.modules["cps.config"] = config_mod
    cps_pkg.config = config_mod

    if "cps.services" not in sys.modules:
        services_pkg = types.ModuleType("cps.services")
        services_pkg.__path__ = [str(REPO_ROOT / "cps" / "services")]
        sys.modules["cps.services"] = services_pkg

    spec = importlib.util.spec_from_file_location(
        "cps.services.cover_extract", REPO_ROOT / "cps" / "services" / "cover_extract.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["cps.services.cover_extract"] = module
    spec.loader.exec_module(module)
    return module


extract = _load_extract_module()


def _make_minimal_epub(tmp_path: Path, cover_bytes: bytes, cover_href: str = "OEBPS/images/cover.jpg") -> Path:
    """Build the smallest spec-conforming EPUB that has a discoverable
    cover image. Used to exercise _extract_from_epub against a real
    zipfile rather than a mock."""
    epub_path = tmp_path / "test.epub"
    container_xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container' version='1.0'>"
        "<rootfiles>"
        "<rootfile full-path='OEBPS/content.opf' media-type='application/oebps-package+xml'/>"
        "</rootfiles>"
        "</container>"
    )
    cover_filename = os.path.basename(cover_href)
    opf_xml = f"""<?xml version='1.0' encoding='UTF-8'?>
<package xmlns='http://www.idpf.org/2007/opf' version='3.0' unique-identifier='id'>
  <metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>
    <dc:title>Test</dc:title>
    <dc:identifier id='id'>test</dc:identifier>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id='cover' href='images/{cover_filename}' media-type='image/jpeg' properties='cover-image'/>
  </manifest>
  <spine/>
</package>"""

    with zipfile.ZipFile(epub_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr("OEBPS/content.opf", opf_xml)
        zf.writestr(cover_href, cover_bytes)
    return epub_path


def _make_cbz(tmp_path: Path, names_and_bytes: list[tuple[str, bytes]]) -> Path:
    cbz_path = tmp_path / "test.cbz"
    with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in names_and_bytes:
            zf.writestr(name, data)
    return cbz_path


@pytest.mark.unit
@pytest.mark.skipif(not extract._LXML_AVAILABLE, reason="lxml not installed (production has it)")
class TestEpubExtraction:
    def test_extracts_cover_from_modern_epub(self, tmp_path):
        cover_payload = b"\xff\xd8\xff\xe0FAKE_JPEG_PAYLOAD"
        epub = _make_minimal_epub(tmp_path, cover_payload)
        result = extract._extract_from_epub(str(epub))
        assert result is not None
        assert result.data == cover_payload
        assert result.extension == ".jpg"
        assert result.source_format == "epub"
        assert result.mime_type == "image/jpeg"

    def test_returns_none_on_corrupt_zip(self, tmp_path):
        bad = tmp_path / "bad.epub"
        bad.write_bytes(b"not a zip file at all")
        assert extract._extract_from_epub(str(bad)) is None

    def test_returns_none_when_manifest_has_no_cover_item(self, tmp_path):
        # Build an EPUB where the manifest has no cover-image properties
        # and no <meta name='cover'>.
        epub = tmp_path / "no-cover.epub"
        with zipfile.ZipFile(epub, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("META-INF/container.xml",
                "<?xml version='1.0'?><container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>"
                "<rootfiles><rootfile full-path='content.opf' media-type='application/oebps-package+xml'/></rootfiles>"
                "</container>")
            zf.writestr("content.opf",
                "<?xml version='1.0'?><package xmlns='http://www.idpf.org/2007/opf' version='3.0'>"
                "<metadata/><manifest/><spine/></package>")
        assert extract._extract_from_epub(str(epub)) is None


@pytest.mark.unit
class TestCbzExtraction:
    def test_extracts_first_image_alphabetically(self, tmp_path):
        cbz = _make_cbz(tmp_path, [
            ("page002.jpg", b"page2-data"),
            ("page001.jpg", b"page1-data"),  # this is the cover
            ("page003.jpg", b"page3-data"),
        ])
        result = extract._extract_from_cbz(str(cbz))
        assert result is not None
        assert result.data == b"page1-data"
        assert result.extension == ".jpg"
        assert result.source_format == "cbz"

    def test_skips_non_image_files(self, tmp_path):
        cbz = _make_cbz(tmp_path, [
            ("ComicInfo.xml", b"<xml/>"),
            ("00.jpg", b"jpg-data"),
        ])
        result = extract._extract_from_cbz(str(cbz))
        assert result is not None
        assert result.data == b"jpg-data"

    def test_returns_none_when_no_images(self, tmp_path):
        cbz = _make_cbz(tmp_path, [("ComicInfo.xml", b"<xml/>")])
        assert extract._extract_from_cbz(str(cbz)) is None


@pytest.mark.unit
class TestPdfExtraction:
    def test_returns_none_when_pypdfium2_missing(self, tmp_path):
        # In CI / dev envs where pypdfium2 isn't installed, the function
        # must not raise — it returns None and the picker omits the
        # PDF candidate.
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")
        # If pypdfium2 *is* installed locally, this test still passes
        # because the import works but rendering a fake PDF fails.
        result = extract._extract_from_pdf(str(pdf_path))
        # Either way: no exception, None or successful render.
        assert result is None or result.source_format == "pdf"


@pytest.mark.unit
class TestExtractEmbeddedCover:
    def test_picks_first_supported_format(self, tmp_path, monkeypatch):
        # A book with two formats: CBZ first, EPUB second. The function
        # should iterate book.data and return the first format that
        # yields a cover.
        cbz = _make_cbz(tmp_path, [("01.jpg", b"cbz-cover")])
        epub = _make_minimal_epub(tmp_path, b"\xff\xd8\xff\xe0EPUB_COVER")

        # Stub config.get_book_path() to return "" so we can pass absolute
        # paths via book.path.
        monkeypatch.setattr(extract.config, "get_book_path", lambda: "")

        book = types.SimpleNamespace(
            path="",
            data=[
                types.SimpleNamespace(name=cbz.stem, format="CBZ"),
                types.SimpleNamespace(name=epub.stem, format="EPUB"),
            ],
        )
        # The data entries' name+format reconstruct into the file paths.
        # CBZ comes first; should win.
        # Patch _book_format_path to return our absolute paths directly.
        def fake_path(b, entry):
            if entry.format == "CBZ":
                return str(cbz)
            if entry.format == "EPUB":
                return str(epub)
            return None
        monkeypatch.setattr(extract, "_book_format_path", fake_path)

        result = extract.extract_embedded_cover(book)
        assert result is not None
        assert result.data == b"cbz-cover"  # CBZ extracted first

    def test_returns_none_when_no_supported_formats(self, monkeypatch):
        book = types.SimpleNamespace(
            path="",
            data=[types.SimpleNamespace(name="x", format="MOBI")],
        )
        monkeypatch.setattr(extract, "_book_format_path", lambda b, e: "/nonexistent/x.mobi")
        assert extract.extract_embedded_cover(book) is None

    def test_returns_none_on_empty_book(self):
        book = types.SimpleNamespace(path="", data=[])
        assert extract.extract_embedded_cover(book) is None
