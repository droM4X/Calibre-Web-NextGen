# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Aspect-ratio padding for book covers served to Kobo devices.

Kobo e-ink screens (Libra Color, Libra 2 = 1264x1680; Clara family =
1072x1448) are roughly 3:4. Most publisher cover art is closer to 2:3
(taller and narrower). Modern Kobo firmware fits-to-screen and pads the
mismatch with white bars, which looks cheap on the home screen and the
sleep cover.

This module pads source covers to a target aspect ratio *server-side* so
the device receives an image that already matches its screen and renders
edge-to-edge.

Five fill modes:

    - "edge_mirror"   - mirror the cover's outer edge into the pad area;
                        feels like a natural continuation of the artwork
    - "edge_blur"     - take the outer edge column/row, stretch it across
                        the pad area, blur heavily; soft "bokeh" border
    - "average"       - solid pad, color = average pixel of the cover
    - "dominant"      - solid pad, color = quantized-mode pixel
    - "manual"        - solid pad, color = a hex string supplied by the
                        admin

The pure-functional API takes JPEG bytes and returns JPEG bytes. The
serve-side wrapper adds a filesystem cache keyed by source mtime + a
settings hash so changes in the admin panel invalidate cleanly.

Wand (ImageMagick) is the only image dependency; this module degrades to
a no-op (returns source bytes unchanged) when Wand is unavailable.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Optional, Tuple

from .. import logger

log = logger.create()

try:
    from wand.color import Color
    from wand.image import Image
    use_IM = True
except (ImportError, RuntimeError):  # ImageMagick missing → degrade gracefully
    use_IM = False
    Color = None  # type: ignore
    Image = None  # type: ignore


# Public fill-mode identifiers. Anything else falls through to "edge_mirror".
FILL_MODES = ("edge_mirror", "edge_blur", "average", "dominant", "manual")
DEFAULT_FILL_MODE = "edge_mirror"

# 1264:1680 = Libra Color / Libra 2 native; 1072:1448 = Clara family. We
# default to Libra Color since it has the larger user base in the fork.
PRESET_ASPECTS = {
    "kobo_libra_color": (1264, 1680),
    "kobo_libra_2": (1264, 1680),
    "kobo_clara": (1072, 1448),
}
DEFAULT_PRESET = "kobo_libra_color"

# How close the source must be to the target ratio to skip padding entirely.
_RATIO_EPSILON = 0.005

# Edge-strip width for blur/mirror sampling (pixels). Larger = more of the
# cover's edge bleeds into the pad area; smaller = pad area looks more
# uniform. 24 px is a good middle for ~1500px-tall covers.
_EDGE_STRIP_PX = 24

# JPEG output quality. Matches the thumbnail task's pin.
_JPEG_QUALITY = 88


@dataclass(frozen=True)
class PaddingSettings:
    """Snapshot of the admin's Kobo-padding configuration.

    Hashable + frozen so it can be passed around safely and used as a cache key.
    """
    enabled: bool
    target_aspect: str  # "WxH" or a preset key, e.g. "1264x1680" or "kobo_libra_color"
    fill_mode: str
    manual_color: str  # hex with leading # when fill_mode == "manual"; "" otherwise

    def settings_hash(self) -> str:
        """Stable short hash of the settings tuple. Used in cache filenames
        and (optionally) appended to CoverImageId for device-side cache
        invalidation when settings change.

        Only hashes fields that actually affect rendered output:
        manual_color is ignored unless fill_mode is "manual", so toggling
        it while in another mode doesn't bust the cache.
        """
        if not self.enabled:
            return "off"
        effective_color = self.manual_color or "" if self.fill_mode == "manual" else ""
        parts = [self.target_aspect, self.fill_mode, effective_color]
        digest = hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()
        return digest[:10]


def parse_target_ratio(target_aspect: str) -> float:
    """Resolve a preset name or 'WxH' string to a float ratio (W/H).

    Falls back to the Libra Color default if the input is malformed.
    """
    if not target_aspect:
        w, h = PRESET_ASPECTS[DEFAULT_PRESET]
        return w / h

    if target_aspect in PRESET_ASPECTS:
        w, h = PRESET_ASPECTS[target_aspect]
        return w / h

    # WxH form, e.g. "1264x1680" or "1264:1680"
    sep = "x" if "x" in target_aspect else (":" if ":" in target_aspect else None)
    if sep:
        try:
            w_str, h_str = target_aspect.split(sep, 1)
            w, h = int(w_str.strip()), int(h_str.strip())
            if w > 0 and h > 0:
                return w / h
        except (ValueError, TypeError):
            pass

    log.warning("cover_padding: unrecognized target_aspect %r, using default", target_aspect)
    w, h = PRESET_ASPECTS[DEFAULT_PRESET]
    return w / h


def _hex_to_color(hex_str: str):
    """Wand Color object from '#RRGGBB' (or fallback to white on bad input)."""
    if not hex_str:
        return Color("white")
    candidate = hex_str.strip()
    if not candidate.startswith("#"):
        candidate = "#" + candidate
    try:
        return Color(candidate)
    except Exception:  # Wand raises ValueError-likes for unparseable colors
        log.warning("cover_padding: invalid manual color %r, using white", hex_str)
        return Color("white")


def _color_to_hex(c) -> str:
    """Wand Color → '#rrggbb'. Wand exposes 0-1 floats per channel."""
    r = max(0, min(255, int(round(c.red * 255))))
    g = max(0, min(255, int(round(c.green * 255))))
    b = max(0, min(255, int(round(c.blue * 255))))
    return "#{:02x}{:02x}{:02x}".format(r, g, b)


def average_color_hex(img) -> str:
    """Mean color across the image. Cheap: resize-to-1x1 lets ImageMagick do
    the averaging in C."""
    with img.clone() as scaled:
        scaled.resize(1, 1)
        return _color_to_hex(scaled[0, 0])


def dominant_color_hex(img) -> str:
    """Quantized-mode color. Resize to 64x64, quantize to 8 colors, pick the
    bucket with the most pixels. Avoids histogram-of-full-image cost while
    still capturing the visual mode."""
    with img.clone() as small:
        small.resize(64, 64)
        try:
            small.quantize(8, dither=False)
        except Exception:
            # Older Wand versions may not accept all quantize kwargs; the
            # average still works as a fallback.
            return average_color_hex(img)
        hist = small.histogram
        if not hist:
            return average_color_hex(img)
        winner = max(hist.items(), key=lambda kv: kv[1])[0]
        return _color_to_hex(winner)


def _compute_padded_dims(src_w: int, src_h: int, target_ratio: float) -> Tuple[int, int, str]:
    """Return (new_w, new_h, orient) where orient is 'horizontal' (left+right
    pads) or 'vertical' (top+bottom pads). 'noop' if already on target."""
    if src_w <= 0 or src_h <= 0:
        return src_w, src_h, "noop"
    src_ratio = src_w / src_h
    if abs(src_ratio - target_ratio) < _RATIO_EPSILON:
        return src_w, src_h, "noop"
    if src_ratio > target_ratio:
        # too wide → pad top + bottom
        return src_w, int(round(src_w / target_ratio)), "vertical"
    # too tall → pad left + right
    return int(round(src_h * target_ratio)), src_h, "horizontal"


def _composite_solid(img, new_w: int, new_h: int, orient: str, fill_color):
    """Solid-color pad. Returns a new Image owned by the caller (must close)."""
    canvas = Image(width=new_w, height=new_h, background=fill_color)
    if orient == "vertical":
        offset_x, offset_y = 0, (new_h - img.height) // 2
    else:
        offset_x, offset_y = (new_w - img.width) // 2, 0
    canvas.composite(img, left=offset_x, top=offset_y)
    return canvas


def _tile_strip(canvas, strip, axis_origin: int, axis_size: int, orient: str):
    """Tile a strip across `axis_size` px, starting at axis_origin. The strip
    is composited side-by-side (orient='horizontal') or stacked
    (orient='vertical') for cases where the pad area is wider/taller than
    the source strip's dimension."""
    if orient == "horizontal":
        # tile horizontally: strip.width steps along x
        x = axis_origin
        end = axis_origin + axis_size
        while x < end:
            canvas.composite(strip, left=x, top=0)
            x += max(1, strip.width)
    else:
        y = axis_origin
        end = axis_origin + axis_size
        while y < end:
            canvas.composite(strip, left=0, top=y)
            y += max(1, strip.height)


def _composite_edge_mirror(img, new_w: int, new_h: int, orient: str):
    """Mirror the source's outer edges into the pad area. Looks like the
    artwork extends naturally into the border."""
    canvas = Image(width=new_w, height=new_h, background=Color("white"))
    if orient == "horizontal":
        pad_total = new_w - img.width
        left_pad = pad_total // 2
        right_pad = pad_total - left_pad
        # Left pad: leftmost min(left_pad, img.width) → flop → place
        if left_pad > 0:
            strip_w = min(left_pad, img.width)
            with img.clone() as strip:
                strip.crop(left=0, top=0, width=strip_w, height=img.height)
                strip.flop()
                if strip.width >= left_pad:
                    canvas.composite(strip, left=0, top=0)
                else:
                    _tile_strip(canvas, strip, 0, left_pad, "horizontal")
        if right_pad > 0:
            strip_w = min(right_pad, img.width)
            with img.clone() as strip:
                strip.crop(left=img.width - strip_w, top=0, width=strip_w, height=img.height)
                strip.flop()
                start = left_pad + img.width
                if strip.width >= right_pad:
                    canvas.composite(strip, left=start, top=0)
                else:
                    _tile_strip(canvas, strip, start, right_pad, "horizontal")
        canvas.composite(img, left=left_pad, top=0)
    else:  # vertical
        pad_total = new_h - img.height
        top_pad = pad_total // 2
        bottom_pad = pad_total - top_pad
        if top_pad > 0:
            strip_h = min(top_pad, img.height)
            with img.clone() as strip:
                strip.crop(left=0, top=0, width=img.width, height=strip_h)
                strip.flip()
                if strip.height >= top_pad:
                    canvas.composite(strip, left=0, top=0)
                else:
                    _tile_strip(canvas, strip, 0, top_pad, "vertical")
        if bottom_pad > 0:
            strip_h = min(bottom_pad, img.height)
            with img.clone() as strip:
                strip.crop(left=0, top=img.height - strip_h, width=img.width, height=strip_h)
                strip.flip()
                start = top_pad + img.height
                if strip.height >= bottom_pad:
                    canvas.composite(strip, left=0, top=start)
                else:
                    _tile_strip(canvas, strip, start, bottom_pad, "vertical")
        canvas.composite(img, left=0, top=top_pad)
    return canvas


def _composite_edge_blur(img, new_w: int, new_h: int, orient: str):
    """Stretch the outer edge across the pad area and blur heavily. A softer,
    bokeh-like alternative to edge_mirror that hides edge artifacts."""
    canvas = Image(width=new_w, height=new_h, background=Color("white"))
    if orient == "horizontal":
        pad_total = new_w - img.width
        left_pad = pad_total // 2
        right_pad = pad_total - left_pad
        sample_w = max(2, min(_EDGE_STRIP_PX, img.width))
        if left_pad > 0:
            with img.clone() as strip:
                strip.crop(left=0, top=0, width=sample_w, height=img.height)
                strip.resize(left_pad, img.height)
                strip.blur(radius=0, sigma=20)
                canvas.composite(strip, left=0, top=0)
        if right_pad > 0:
            with img.clone() as strip:
                strip.crop(left=img.width - sample_w, top=0, width=sample_w, height=img.height)
                strip.resize(right_pad, img.height)
                strip.blur(radius=0, sigma=20)
                canvas.composite(strip, left=left_pad + img.width, top=0)
        canvas.composite(img, left=left_pad, top=0)
    else:
        pad_total = new_h - img.height
        top_pad = pad_total // 2
        bottom_pad = pad_total - top_pad
        sample_h = max(2, min(_EDGE_STRIP_PX, img.height))
        if top_pad > 0:
            with img.clone() as strip:
                strip.crop(left=0, top=0, width=img.width, height=sample_h)
                strip.resize(img.width, top_pad)
                strip.blur(radius=0, sigma=20)
                canvas.composite(strip, left=0, top=0)
        if bottom_pad > 0:
            with img.clone() as strip:
                strip.crop(left=0, top=img.height - sample_h, width=img.width, height=sample_h)
                strip.resize(img.width, bottom_pad)
                strip.blur(radius=0, sigma=20)
                canvas.composite(strip, left=0, top=top_pad + img.height)
        canvas.composite(img, left=0, top=top_pad)
    return canvas


def pad_blob(blob: bytes, settings: PaddingSettings) -> bytes:
    """Top-level pure entry point. JPEG bytes in, JPEG bytes out.

    No-ops (returns input unchanged) when:
      - Wand isn't installed
      - settings.enabled is False
      - the source is already at the target ratio (within epsilon)
      - the blob isn't decodable as an image
    """
    if not use_IM or not settings.enabled or not blob:
        return blob

    target_ratio = parse_target_ratio(settings.target_aspect)
    fill_mode = settings.fill_mode if settings.fill_mode in FILL_MODES else DEFAULT_FILL_MODE

    try:
        with Image(blob=blob) as img:
            new_w, new_h, orient = _compute_padded_dims(img.width, img.height, target_ratio)
            if orient == "noop":
                return blob

            # Force RGB so JPEG output is well-formed even if the source was
            # palette-indexed or grayscale.
            try:
                img.transform_colorspace("srgb")
            except Exception:
                pass

            if fill_mode == "manual":
                fill_color = _hex_to_color(settings.manual_color)
                padded = _composite_solid(img, new_w, new_h, orient, fill_color)
            elif fill_mode == "average":
                fill_color = _hex_to_color(average_color_hex(img))
                padded = _composite_solid(img, new_w, new_h, orient, fill_color)
            elif fill_mode == "dominant":
                fill_color = _hex_to_color(dominant_color_hex(img))
                padded = _composite_solid(img, new_w, new_h, orient, fill_color)
            elif fill_mode == "edge_blur":
                padded = _composite_edge_blur(img, new_w, new_h, orient)
            else:  # edge_mirror (default)
                padded = _composite_edge_mirror(img, new_w, new_h, orient)

            try:
                padded.format = "jpeg"
                padded.compression_quality = _JPEG_QUALITY
                out = padded.make_blob()
                return out
            finally:
                padded.close()
    except Exception as ex:
        log.warning("cover_padding: pad_blob failed (%s); returning source", ex)
        return blob


def pad_path_to_cache(
    src_path: str,
    cache_dir: str,
    cache_filename: str,
    settings: PaddingSettings,
) -> Optional[str]:
    """Read src_path, pad, write to cache_dir/cache_filename. Returns the
    cache file path on success (or if the cache hit already exists), None
    on failure or if padding is disabled.

    Caller is responsible for choosing a `cache_filename` that encodes the
    source mtime + settings.settings_hash() so cache invalidation is
    automatic.
    """
    if not use_IM or not settings.enabled or not src_path:
        return None
    if not os.path.isfile(src_path):
        return None

    target = os.path.join(cache_dir, cache_filename)
    if os.path.isfile(target):
        return target

    try:
        with open(src_path, "rb") as fh:
            blob = fh.read()
    except OSError as ex:
        log.warning("cover_padding: cannot read %s: %s", src_path, ex)
        return None

    padded = pad_blob(blob, settings)
    if padded is blob:
        # No-op padding: just symlink-equivalent (copy) so the caller can
        # serve from the cache path without branching. Cheap + simple.
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(target, "wb") as out_fh:
                out_fh.write(blob)
            return target
        except OSError as ex:
            log.warning("cover_padding: cannot write passthrough %s: %s", target, ex)
            return None

    try:
        os.makedirs(cache_dir, exist_ok=True)
        # Atomic-ish: write to .tmp then rename so a partial write never
        # serves to a Kobo mid-sync.
        tmp = target + ".tmp"
        with open(tmp, "wb") as out_fh:
            out_fh.write(padded)
        os.replace(tmp, target)
        return target
    except OSError as ex:
        log.warning("cover_padding: cannot write padded %s: %s", target, ex)
        return None


def cache_filename_for(book_uuid: str, resolution, src_mtime: int, settings: PaddingSettings) -> str:
    """Deterministic cache filename. Encodes everything that could change
    the rendered output."""
    return "kobopad-{uuid}-{res}-{mtime}-{hash}.jpg".format(
        uuid=book_uuid,
        res=int(resolution) if resolution else 0,
        mtime=int(src_mtime),
        hash=settings.settings_hash(),
    )
