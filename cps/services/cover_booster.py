# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Cover-resolution booster for metadata-search results.

Many providers serve thumbnail-sized cover URLs (Hardcover ~290x475, Open
Library "-L" ~500x..., Google Books default ~128 wide). High-DPI e-readers
like the Kobo Libra Color (1264x1680) need ~1500px-wide covers to render
crisply. This module takes the aggregated MetaRecord list from
search_metadata.metadata_search() and, in parallel, looks up a higher-res
alternative for each record, replacing record.cover when one is found.

Sources tried, in order, per record:

  1. iTunes Search API (https://itunes.apple.com/lookup or /search) -
     free, no API key, returns artworkUrl100 which we rewrite to
     1500x1500bb.jpg. Works for the long tail of trade fiction / non-
     fiction sold on Apple Books.
  2. Amazon `_SL1500_` URL rewrite - if the record cover is already an
     Amazon m.media-amazon.com asset with a sizing token, swap to
     _SL1500_ to pull the full-resolution variant.

Disable globally with env CWA_COVER_BOOST=0. Tuning knobs:

  CWA_COVER_BOOST_TIMEOUT  - per-lookup HTTP timeout, seconds (default 4)
  CWA_COVER_BOOST_WORKERS  - thread pool size for parallel lookups (default 8)
  CWA_COVER_BOOST_MAX      - cap on records boosted per request (default 30)
"""
from __future__ import annotations

import concurrent.futures
import os
import re
from typing import Dict, Iterable, List, Optional
from urllib.parse import quote_plus

import requests

from .. import constants, logger


log = logger.create()


_DEFAULT_TIMEOUT = float(os.environ.get("CWA_COVER_BOOST_TIMEOUT", "4"))
_DEFAULT_WORKERS = int(os.environ.get("CWA_COVER_BOOST_WORKERS", "8"))
_DEFAULT_MAX = int(os.environ.get("CWA_COVER_BOOST_MAX", "30"))

# Patterns that indicate the cover URL is already high-res - skip work.
_HIGHRES_HINTS = (
    "_SL1500_", "_SL2000_", "1500x1500bb", "2400x2400bb", "fife=w1600",
    "fife=w2000", "fife=w2400",
)

# Amazon dynamic-image sizing token: ._SX475_., ._SY450_., ._UL320_., etc.
_AMAZON_SIZE_TOKEN = re.compile(r"\._(?:S[XLY]|UL|UY|UX|CR|AC|FM)\d+(?:_,\d+,\d+,\d+,\d+)?_\.")


def boost_covers(records: List[Dict]) -> List[Dict]:
    """Mutate each MetaRecord-as-dict in place, upgrading record["cover"] when a
    higher-resolution variant can be found. Returns the same list for chaining.

    Inputs are dicts (post-asdict()) keyed like MetaRecord: title, authors,
    identifiers (with optional 'isbn'), cover, source, etc. Records with no
    title or no cover are skipped.
    """
    if os.environ.get("CWA_COVER_BOOST", "1").lower() in ("0", "false", "no", "off"):
        return records
    if not records:
        return records

    candidates: List[Dict] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        cover = rec.get("cover") or ""
        if not rec.get("title") or not cover:
            continue
        if any(h in cover for h in _HIGHRES_HINTS):
            continue
        candidates.append(rec)
        if len(candidates) >= _DEFAULT_MAX:
            break

    if not candidates:
        return records

    workers = max(1, min(_DEFAULT_WORKERS, len(candidates)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_boosted_cover_for, rec): rec for rec in candidates}
        for future in concurrent.futures.as_completed(futures):
            rec = futures[future]
            try:
                upgraded = future.result()
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("cover boost: lookup failed for %r: %s", rec.get("title"), exc)
                continue
            if upgraded:
                log.debug(
                    "cover boost: %s -> %s (was %s)",
                    rec.get("title"), upgraded, rec.get("cover"),
                )
                rec["cover"] = upgraded
    return records


def _boosted_cover_for(record: Dict) -> Optional[str]:
    """Return a higher-resolution cover URL for ``record`` or None."""
    title = (record.get("title") or "").strip()
    authors = record.get("authors") or []
    primary_author = (authors[0] if authors else "") or ""
    isbn = _isbn_from(record.get("identifiers") or {})

    # Path 1: iTunes lookup by ISBN. Apple's catalog occasionally cross-
    # references unrelated books to the same ISBN (collections, omnibuses,
    # mis-tagged anthologies), so we still verify the returned trackName
    # matches the record's title before swapping the cover.
    if isbn and title:
        result = _itunes_lookup_isbn(isbn)
        if result and _itunes_result_matches(title, primary_author, result):
            url = _itunes_artwork(result)
            if url:
                return url

    # Path 2: iTunes search by title + first author.
    if title:
        result = _itunes_search(title, primary_author)
        if result and _itunes_result_matches(title, primary_author, result):
            url = _itunes_artwork(result)
            if url:
                return url

    # Path 3: Amazon URL rewrite (works only if the current cover is already
    # an Amazon image but at a small sizing token).
    current = record.get("cover") or ""
    if "m.media-amazon.com/images/" in current or "ssl-images-amazon.com/images/" in current:
        rewritten = _AMAZON_SIZE_TOKEN.sub("._SL1500_.", current)
        if rewritten != current:
            return rewritten

    return None


def _isbn_from(identifiers: Dict) -> Optional[str]:
    for key in ("isbn", "isbn_13", "isbn13", "isbn_10", "isbn10"):
        val = identifiers.get(key)
        if val:
            digits = re.sub(r"[^0-9Xx]", "", str(val))
            if len(digits) in (10, 13):
                return digits
    return None


def _itunes_lookup_isbn(isbn: str) -> Optional[Dict]:
    try:
        resp = requests.get(
            "https://itunes.apple.com/lookup",
            params={"isbn": isbn, "country": "us", "media": "ebook"},
            headers={"User-Agent": getattr(constants, "USER_AGENT", "Calibre-Web")},
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json() or {}
    except (requests.RequestException, ValueError):
        return None
    results = payload.get("results") or []
    return results[0] if results else None


def _itunes_search(title: str, author: str) -> Optional[Dict]:
    term = f"{title} {author}".strip()
    if not term:
        return None
    try:
        resp = requests.get(
            "https://itunes.apple.com/search",
            params={
                "term": term,
                "country": "us",
                "media": "ebook",
                "entity": "ebook",
                "limit": 3,
            },
            headers={"User-Agent": getattr(constants, "USER_AGENT", "Calibre-Web")},
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json() or {}
    except (requests.RequestException, ValueError):
        return None
    # Apple occasionally returns audiobook/wrapper entries despite media=ebook.
    for result in payload.get("results") or []:
        kind = result.get("kind") or result.get("wrapperType")
        if kind in ("ebook", "audiobook"):
            return result
    return None


def _itunes_result_matches(query_title: str, query_author: str, result: Dict) -> bool:
    """Conservative fuzzy check that the iTunes hit is for the same book.

    Avoids replacing covers with unrelated images when the search engine
    returns a loose match. Title token overlap >=70% with first 6 tokens.
    """
    if not result:
        return False
    track = (result.get("trackName") or result.get("collectionName") or "").lower()
    if not track:
        return False
    qtokens = _tokenize(query_title)[:6]
    if not qtokens:
        return False
    rtokens = set(_tokenize(track))
    overlap = sum(1 for t in qtokens if t in rtokens) / float(len(qtokens))
    if overlap < 0.7:
        return False
    if query_author:
        artist = (result.get("artistName") or "").lower()
        atokens = _tokenize(query_author)
        if atokens and not any(t in artist for t in atokens):
            return False
    return True


def _tokenize(text: str) -> List[str]:
    return [tok for tok in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(tok) > 2]


def _itunes_artwork(result: Optional[Dict]) -> Optional[str]:
    """Extract artwork URL from an iTunes result and upgrade to 1500px.

    iTunes returns artworkUrl100 / artworkUrl60 with a path segment like
    ``100x100bb.jpg``; their CDN serves arbitrary sizes when you replace
    that segment. 1500x1500bb is the sweet spot for Kobo Libra Color.
    """
    if not result:
        return None
    art = (
        result.get("artworkUrl100")
        or result.get("artworkUrl512")
        or result.get("artworkUrl60")
        or result.get("artworkUrl30")
    )
    if not art:
        return None
    upgraded = re.sub(r"/\d+x\d+bb\.jpg$", "/1500x1500bb.jpg", art)
    upgraded = re.sub(r"/\d+x\d+bb\.png$", "/1500x1500bb.png", upgraded)
    return upgraded if upgraded.startswith("http") else None
