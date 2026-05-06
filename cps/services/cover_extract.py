# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Extract the embedded cover from a book file already in the library.

Used by the cover-picker page to surface "the current embedded cover" as
a candidate alongside the network sources. Resolves the recurring user
ask in CWA #1063 + janeczku/calibre-web#3457 for a "regenerate cover
from the file" path.

Returns raw image bytes (and the detected extension) so the caller can
either serve them as a data URL in the picker grid OR pipe them through
``helper.save_cover`` on apply. No new dependencies — only reads ZIP
archives via the stdlib ``zipfile`` module the way ``cps/epub.py`` does.

Supported formats (best-effort; missing-format == no candidate, never raises):
  - EPUB / KEPUB (zipfile + manifest cover-image item)
  - CBZ / CB7 (first image in the archive — same convention as comic.py)
  - PDF (defers to PyPDF / pikepdf if installed; returns None otherwise)

MOBI / AZW3 are deliberately not supported here; the extraction libraries
add weight and the user can always upload a file manually if the embedded
cover matters for those formats.
"""
from __future__ import annotations

import dataclasses
import os
import zipfile
from typing import Optional

try:
    from lxml import etree as _etree
    _LXML_AVAILABLE = True
except ImportError:  # pragma: no cover - dev envs without lxml
    _etree = None
    _LXML_AVAILABLE = False

from .. import config, logger


log = logger.create()


_EPUB_NS = {
    "n": "urn:oasis:names:tc:opendocument:xmlns:container",
    "pkg": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


@dataclasses.dataclass
class ExtractedCover:
    """Raw embedded cover bytes plus the detected file extension. The
    cover-picker page serves this as a data URL; ``apply`` re-uses the
    bytes through the existing ``helper.save_cover`` path."""

    data: bytes
    extension: str  # lowercase, with leading dot, e.g. ".jpg"
    source_format: str  # "epub" | "cbz" | "pdf"

    @property
    def mime_type(self) -> str:
        return {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp",
            ".gif": "image/gif", ".bmp": "image/bmp",
        }.get(self.extension, "application/octet-stream")


def extract_embedded_cover(book) -> Optional[ExtractedCover]:
    """Extract the cover from any of ``book.data`` formats. Tries EPUB
    first (highest signal), then CBZ, then PDF.

    Never raises. Returns None if no cover can be extracted, the file is
    missing, or the format isn't supported here. Logs at DEBUG."""
    formats = list(book.data) if hasattr(book, "data") else []
    for entry in formats:
        ext = (entry.format or "").lower()
        path = _book_format_path(book, entry)
        if not path or not os.path.isfile(path):
            continue
        if ext in ("epub", "kepub"):
            cover = _extract_from_epub(path)
            if cover is not None:
                return cover
        elif ext in ("cbz", "cb7"):
            cover = _extract_from_cbz(path)
            if cover is not None:
                return cover
        elif ext == "pdf":
            cover = _extract_from_pdf(path)
            if cover is not None:
                return cover
    return None


def _book_format_path(book, data_entry) -> Optional[str]:
    """Build the on-disk path to a particular format of ``book``."""
    book_dir = getattr(book, "path", None)
    name = getattr(data_entry, "name", None)
    fmt = getattr(data_entry, "format", "")
    if not (book_dir and name and fmt):
        return None
    base = config.get_book_path() if config else ""
    return os.path.join(base, book_dir, f"{name}.{fmt.lower()}")


def _extract_from_epub(file_path: str) -> Optional[ExtractedCover]:
    """Pull the cover out of an EPUB by reading the OPF manifest. Mirrors
    ``cps.epub._extract_cover``'s detection logic but reads bytes
    directly instead of writing them through cover_processing."""
    if not _LXML_AVAILABLE:
        log.debug("lxml not installed; skipping EPUB cover extract for %s", file_path)
        return None
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            container = zf.read("META-INF/container.xml")
            container_tree = _etree.fromstring(container)
            opf_path = container_tree.xpath(
                "n:rootfiles/n:rootfile/@full-path", namespaces=_EPUB_NS,
            )
            if not opf_path:
                return None
            opf_data = zf.read(opf_path[0])
            opf_tree = _etree.fromstring(opf_data)

            cover_href = _find_epub_cover_href(opf_tree)
            if not cover_href:
                return None

            opf_dir = os.path.dirname(opf_path[0])
            cover_zip_path = os.path.join(opf_dir, cover_href).replace("\\", "/")
            try:
                cover_bytes = zf.read(cover_zip_path)
            except KeyError:
                return None
            ext = os.path.splitext(cover_href)[1].lower()
            if ext not in _IMAGE_EXTS:
                return None
            return ExtractedCover(data=cover_bytes, extension=ext, source_format="epub")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        log.debug("EPUB cover extraction failed for %s: %s", file_path, exc)
        return None
    except Exception as exc:  # lxml-specific errors at runtime
        log.debug("EPUB cover XML parse failed for %s: %s", file_path, exc)
        return None


def _find_epub_cover_href(opf_tree) -> Optional[str]:
    """Apply the same fallback chain ``cps.epub`` does to find the cover
    item href: cover-image id → meta cover content → guide reference."""
    # Modern EPUBs: properties="cover-image"
    href = opf_tree.xpath(
        "//pkg:manifest/pkg:item[@properties='cover-image']/@href",
        namespaces=_EPUB_NS,
    )
    if href:
        return href[0]
    # Older EPUBs: id='cover-image'
    href = opf_tree.xpath(
        "//pkg:manifest/pkg:item[@id='cover-image']/@href",
        namespaces=_EPUB_NS,
    )
    if href:
        return href[0]
    # Even older EPUBs: <meta name="cover" content="<item-id>"/>
    meta_cover = opf_tree.xpath(
        "//pkg:metadata/pkg:meta[@name='cover']/@content",
        namespaces=_EPUB_NS,
    )
    if meta_cover:
        href = opf_tree.xpath(
            f"//pkg:manifest/pkg:item[@id='{meta_cover[0]}']/@href",
            namespaces=_EPUB_NS,
        )
        if href:
            return href[0]
    # Last-ditch: <guide><reference type="cover" href="..."/></guide>
    href = opf_tree.xpath(
        "//pkg:guide/pkg:reference[@type='cover']/@href",
        namespaces=_EPUB_NS,
    )
    return href[0] if href else None


def _extract_from_cbz(file_path: str) -> Optional[ExtractedCover]:
    """Comic-archive cover = first image when sorted alphabetically. Same
    convention ``cps.comic`` follows; CBR (rar) is intentionally
    skipped — it needs the unrar binary and the cover-picker is a
    convenience surface, not a critical path."""
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            image_names = sorted(
                n for n in zf.namelist()
                if os.path.splitext(n)[1].lower() in _IMAGE_EXTS
                and not n.endswith("/")
            )
            if not image_names:
                return None
            first = image_names[0]
            return ExtractedCover(
                data=zf.read(first),
                extension=os.path.splitext(first)[1].lower(),
                source_format="cbz",
            )
    except (zipfile.BadZipFile, OSError) as exc:
        log.debug("CBZ cover extraction failed for %s: %s", file_path, exc)
        return None


def _extract_from_pdf(file_path: str) -> Optional[ExtractedCover]:
    """First-page render of a PDF as a JPEG. Requires ``pdf2image`` or
    ``pypdfium2`` — both are heavy native deps; we don't add them just
    for this. Skipped silently if neither is importable."""
    try:
        import pypdfium2 as pdfium  # type: ignore
    except ImportError:
        log.debug("pypdfium2 not installed; skipping PDF cover extract for %s", file_path)
        return None
    try:
        pdf = pdfium.PdfDocument(file_path)
        if len(pdf) == 0:
            return None
        page = pdf[0]
        bitmap = page.render(scale=2.0)
        image = bitmap.to_pil()
        import io
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=88)
        return ExtractedCover(
            data=buf.getvalue(), extension=".jpg", source_format="pdf",
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("PDF cover render failed for %s: %s", file_path, exc)
        return None
