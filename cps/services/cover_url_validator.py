# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Validate a cover URL before it gets committed.

Used by two callers:

  1. ``POST /metadata/cover/preview`` — the live-preview endpoint behind
     the inline ``cover_url`` field on the edit-metadata page.
  2. The cover-picker page's URL-paste panel.

Both want the same answer: does this URL serve a real image, what are its
dimensions, what's its content-type, is the size within our limits, would
the SSRF guard let us fetch it later. The validator runs the same checks
``helper.save_cover_from_url`` does on the save path so previewing is
faithful to what would actually happen on commit.

Pure-functional. No Flask state. No DB writes. Easy to test with mocked
HTTP. No new dependencies — uses the existing ``cw_advocate`` SSRF guard
and ``Pillow`` (already in requirements via ``cps.helper``).
"""
from __future__ import annotations

import dataclasses
import io
import os
from typing import Optional

try:
    from .. import cw_advocate
    from ..cw_advocate.exceptions import UnacceptableAddressException
    _ADVOCATE_AVAILABLE = True
except ImportError:  # pragma: no cover - dev/test envs without advocate
    cw_advocate = None
    UnacceptableAddressException = type("UnacceptableAddressException", (Exception,), {})
    _ADVOCATE_AVAILABLE = False

try:
    from PIL import Image as _PILImage
except ImportError:  # pragma: no cover - dev envs without Pillow
    _PILImage = None

import requests

from .. import logger


log = logger.create()

_DEFAULT_TIMEOUT = float(os.environ.get("CWA_COVER_PICKER_TIMEOUT", "4"))
_DEFAULT_MAX_BYTES = int(os.environ.get("CWA_COVER_DOWNLOAD_MAX_BYTES", str(15 * 1024 * 1024)))
# A sane minimum — anything below 5 KB is almost certainly a placeholder
# (Amazon's 43-byte image/gif, OL's blank cover, etc.).
_MIN_BYTES = 5_000
# How many bytes to read from the body for dimension detection. Most JPEG
# headers fit in the first 4 KB. PNG IHDR is at offset 16. WebP is at 26.
_DIM_PROBE_BYTES = 8 * 1024
_ACCEPTED_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp")


@dataclasses.dataclass
class ValidationResult:
    """The shape returned by :func:`validate_cover_url` and serialized to
    JSON by the live-preview endpoint."""

    valid: bool
    url: str
    error_code: Optional[str] = None      # machine-readable: 'ssrf_blocked', 'too_small', ...
    error_message: Optional[str] = None   # human-readable, safe to show in the UI
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def validate_cover_url(url: str) -> ValidationResult:
    """Probe ``url`` and return whether it would succeed if saved as a cover.

    The probe is lightweight: a HEAD first to check status + content-type +
    size, then a partial GET to read dimensions out of the image header.
    Total network bytes are bounded by ``_DIM_PROBE_BYTES``. The HEAD probe
    obeys the same SSRF guard the save path uses, so URLs that would fail
    on commit (localhost, RFC1918) fail at preview time too.
    """
    url = (url or "").strip()
    if not url:
        return ValidationResult(valid=False, url=url, error_code="empty",
                                error_message="Enter a URL.")
    if not (url.startswith("http://") or url.startswith("https://")):
        return ValidationResult(valid=False, url=url, error_code="bad_scheme",
                                error_message="URL must start with http:// or https://.")

    if not _ADVOCATE_AVAILABLE or cw_advocate is None:
        return ValidationResult(valid=False, url=url, error_code="advocate_missing",
                                error_message="Cover-URL validation is unavailable on this server.")

    try:
        head = cw_advocate.head(url, timeout=_DEFAULT_TIMEOUT, allow_redirects=True)
    except UnacceptableAddressException:
        return ValidationResult(valid=False, url=url, error_code="ssrf_blocked",
                                error_message="That URL points to an internal or local address. Use a public URL.")
    except (requests.RequestException, Exception) as exc:  # pragma: no cover - defensive
        log.debug("validate_cover_url HEAD failed for %s: %s", url, exc)
        return ValidationResult(valid=False, url=url, error_code="unreachable",
                                error_message="Could not reach that URL. Check the address and try again.")

    if head.status_code != 200:
        return ValidationResult(valid=False, url=url, error_code="bad_status",
                                error_message=f"Server returned HTTP {head.status_code}.")

    content_type = (head.headers.get("content-type") or "").split(";")[0].strip().lower()
    try:
        size_bytes = int(head.headers.get("content-length") or 0)
    except (TypeError, ValueError):
        size_bytes = 0

    if content_type and not any(content_type.startswith(t) for t in _ACCEPTED_TYPES):
        return ValidationResult(
            valid=False, url=url, error_code="not_image",
            error_message=f"That URL serves {content_type or 'an unknown content type'}, not an image.",
            content_type=content_type, size_bytes=size_bytes or None,
        )

    if size_bytes and size_bytes > _DEFAULT_MAX_BYTES:
        return ValidationResult(
            valid=False, url=url, error_code="too_large",
            error_message=f"Image is {size_bytes // (1024 * 1024)} MB; the server limit is "
                          f"{_DEFAULT_MAX_BYTES // (1024 * 1024)} MB.",
            content_type=content_type, size_bytes=size_bytes,
        )

    if size_bytes and size_bytes < _MIN_BYTES:
        return ValidationResult(
            valid=False, url=url, error_code="too_small",
            error_message=f"Image is only {size_bytes} bytes. That's almost always a placeholder, not a real cover.",
            content_type=content_type, size_bytes=size_bytes,
        )

    width, height = _probe_dimensions(url)

    return ValidationResult(
        valid=True, url=url, content_type=content_type or None,
        size_bytes=size_bytes or None, width=width, height=height,
    )


def _probe_dimensions(url: str) -> tuple[Optional[int], Optional[int]]:
    """Stream the first ``_DIM_PROBE_BYTES`` of ``url`` and parse image
    dimensions out of the header. Returns (None, None) if dimensions
    cannot be determined — never raises."""
    if _PILImage is None or cw_advocate is None:
        return None, None
    try:
        resp = cw_advocate.get(url, timeout=_DEFAULT_TIMEOUT, stream=True, allow_redirects=True)
        resp.raise_for_status()
        head_bytes = b""
        for chunk in resp.iter_content(chunk_size=2048):
            head_bytes += chunk
            if len(head_bytes) >= _DIM_PROBE_BYTES:
                break
        resp.close()
    except Exception as exc:
        log.debug("_probe_dimensions stream failed for %s: %s", url, exc)
        return None, None

    if not head_bytes:
        return None, None
    try:
        with _PILImage.open(io.BytesIO(head_bytes)) as img:
            return img.width, img.height
    except Exception as exc:
        log.debug("_probe_dimensions PIL parse failed for %s: %s", url, exc)
        return None, None
