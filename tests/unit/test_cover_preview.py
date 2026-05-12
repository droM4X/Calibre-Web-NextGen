# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for the cover-preview service.

Pure-function tests run anywhere. The image-manipulation tests are gated
on Wand/ImageMagick being installed (`use_IM` flag) — in CI the image
suite runs in the container where ImageMagick is present; locally on a
dev box without ImageMagick we still get the math + hash + dim tests.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import types
from pathlib import Path

import pytest


def _load_preview_module():
    """Idempotently top up the cps stub so this test co-exists with
    sibling service tests."""
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "cps" / "services" / "cover_preview.py"

    cps_pkg = sys.modules.get("cps")
    if cps_pkg is None:
        cps_pkg = types.ModuleType("cps")
        cps_pkg.__path__ = [str(repo_root / "cps")]
        sys.modules["cps"] = cps_pkg

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

    if "cps.services" not in sys.modules:
        services_pkg = types.ModuleType("cps.services")
        services_pkg.__path__ = [str(repo_root / "cps" / "services")]
        sys.modules["cps.services"] = services_pkg

    spec = importlib.util.spec_from_file_location(
        "cps.services.cover_preview", module_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["cps.services.cover_preview"] = module
    spec.loader.exec_module(module)
    return module


preview = _load_preview_module()


# ---------------------------------------------------------------------------
# Pure-function tests (no Wand required)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestParseTargetRatio:
    def test_libra_color_preset(self):
        ratio = preview.parse_target_ratio("kobo_libra_color")
        assert abs(ratio - 1264 / 1680) < 1e-6

    def test_clara_preset(self):
        ratio = preview.parse_target_ratio("kobo_clara")
        assert abs(ratio - 1072 / 1448) < 1e-6

    def test_wxh_string(self):
        assert abs(preview.parse_target_ratio("3x4") - 0.75) < 1e-6
        assert abs(preview.parse_target_ratio("1264x1680") - 1264 / 1680) < 1e-6

    def test_colon_separator(self):
        assert abs(preview.parse_target_ratio("3:4") - 0.75) < 1e-6
        assert abs(preview.parse_target_ratio("2:3") - 2 / 3) < 1e-6

    def test_empty_falls_back_to_default(self):
        ratio = preview.parse_target_ratio("")
        assert abs(ratio - 1264 / 1680) < 1e-6

    def test_garbage_falls_back_to_default(self):
        ratio = preview.parse_target_ratio("not-a-ratio")
        assert abs(ratio - 1264 / 1680) < 1e-6

    def test_zero_falls_back_to_default(self):
        ratio = preview.parse_target_ratio("0x100")
        assert abs(ratio - 1264 / 1680) < 1e-6


@pytest.mark.unit
class TestComputePaddedDims:
    def test_already_at_target_returns_noop(self):
        # 3:4 source, 3:4 target → no pad needed
        new_w, new_h, orient = preview._compute_padded_dims(1500, 2000, 0.75)
        assert orient == "noop"
        assert (new_w, new_h) == (1500, 2000)

    def test_taller_than_target_pads_horizontally(self):
        # 2:3 source (taller, narrower) into 3:4 (wider) → pad left+right
        new_w, new_h, orient = preview._compute_padded_dims(1000, 1500, 0.75)
        assert orient == "horizontal"
        assert new_h == 1500
        assert abs(new_w / new_h - 0.75) < 0.001

    def test_wider_than_target_pads_vertically(self):
        # 4:3 source into 3:4 → pad top+bottom
        new_w, new_h, orient = preview._compute_padded_dims(2000, 1500, 0.75)
        assert orient == "vertical"
        assert new_w == 2000
        assert abs(new_w / new_h - 0.75) < 0.001

    def test_zero_dimensions_noop(self):
        new_w, new_h, orient = preview._compute_padded_dims(0, 0, 0.75)
        assert orient == "noop"

    def test_within_epsilon_is_noop(self):
        # 0.751 vs 0.75 — within 0.005 epsilon → noop
        _, _, orient = preview._compute_padded_dims(751, 1000, 0.75)
        assert orient == "noop"
        # 0.7524 (Libra native) vs 0.75 (3:4) — diff 0.0024 < 0.005 → also noop
        _, _, orient = preview._compute_padded_dims(1264, 1680, 0.75)
        assert orient == "noop"
        # 0.667 (2:3 publisher) vs 0.7524 (Libra) — clearly not noop
        _, _, orient = preview._compute_padded_dims(1000, 1500, 1264 / 1680)
        assert orient != "noop"


@pytest.mark.unit
class TestCoverPreviewSettings:
    def test_disabled_hash_is_off(self):
        s = preview.CoverPreviewSettings(
            enabled=False, target_aspect="kobo_libra_color",
            fill_mode="edge_mirror", manual_color="",
        )
        assert s.settings_hash() == "off"

    def test_hash_changes_when_aspect_changes(self):
        s1 = preview.CoverPreviewSettings(True, "kobo_libra_color", "edge_mirror", "")
        s2 = preview.CoverPreviewSettings(True, "kobo_clara", "edge_mirror", "")
        assert s1.settings_hash() != s2.settings_hash()

    def test_hash_changes_when_fill_mode_changes(self):
        s1 = preview.CoverPreviewSettings(True, "kobo_libra_color", "edge_mirror", "")
        s2 = preview.CoverPreviewSettings(True, "kobo_libra_color", "edge_blur", "")
        assert s1.settings_hash() != s2.settings_hash()

    def test_hash_changes_when_manual_color_changes(self):
        s1 = preview.CoverPreviewSettings(True, "kobo_libra_color", "manual", "#000000")
        s2 = preview.CoverPreviewSettings(True, "kobo_libra_color", "manual", "#ffffff")
        assert s1.settings_hash() != s2.settings_hash()

    def test_manual_color_irrelevant_when_mode_not_manual(self):
        # Cache hit-rate matters: changing the manual color while in a
        # non-manual mode should NOT bust the cache.
        s1 = preview.CoverPreviewSettings(True, "kobo_libra_color", "edge_mirror", "")
        s2 = preview.CoverPreviewSettings(True, "kobo_libra_color", "edge_mirror", "#1a1a1a")
        assert s1.settings_hash() == s2.settings_hash()

    def test_hash_is_short_and_stable(self):
        s = preview.CoverPreviewSettings(True, "kobo_libra_color", "edge_mirror", "")
        h1 = s.settings_hash()
        h2 = s.settings_hash()
        assert h1 == h2
        assert len(h1) == 10
        assert all(c in "0123456789abcdef" for c in h1)


@pytest.mark.unit
class TestCacheFilename:
    def test_filename_encodes_all_inputs(self):
        s = preview.CoverPreviewSettings(True, "kobo_libra_color", "edge_mirror", "")
        name = preview.cache_filename_for("uuid-1234", 4, 1700000000, s)
        assert name.startswith("kobopad-uuid-1234-")
        assert name.endswith(".jpg")
        assert "1700000000" in name
        assert s.settings_hash() in name

    def test_different_mtime_produces_different_filename(self):
        s = preview.CoverPreviewSettings(True, "kobo_libra_color", "edge_mirror", "")
        a = preview.cache_filename_for("u", 4, 1, s)
        b = preview.cache_filename_for("u", 4, 2, s)
        assert a != b

    def test_different_settings_produce_different_filename(self):
        s1 = preview.CoverPreviewSettings(True, "kobo_libra_color", "edge_mirror", "")
        s2 = preview.CoverPreviewSettings(True, "kobo_libra_color", "edge_blur", "")
        a = preview.cache_filename_for("u", 4, 1, s1)
        b = preview.cache_filename_for("u", 4, 1, s2)
        assert a != b


# ---------------------------------------------------------------------------
# Wand-dependent tests — only run when ImageMagick is on the box.
# ---------------------------------------------------------------------------

requires_wand = pytest.mark.skipif(
    not preview.use_IM, reason="Wand/ImageMagick not available in this environment"
)


def _make_test_jpeg(width: int, height: int, color: str = "red") -> bytes:
    """Synthesize a tiny solid-color JPEG via Wand. Used as a fixture."""
    from wand.color import Color
    from wand.image import Image
    with Image(width=width, height=height, background=Color(color)) as img:
        img.format = "jpeg"
        return img.make_blob()


@pytest.mark.unit
@requires_wand
class TestPadBlobShape:
    def test_disabled_settings_returns_input(self):
        blob = _make_test_jpeg(100, 150)
        s = preview.CoverPreviewSettings(False, "kobo_libra_color", "edge_mirror", "")
        out = preview.pad_blob(blob, s)
        assert out == blob

    def test_already_at_target_ratio_returns_input(self):
        # 3:4 source into 3:4 target → no-op
        blob = _make_test_jpeg(300, 400)
        s = preview.CoverPreviewSettings(True, "3:4", "edge_mirror", "")
        out = preview.pad_blob(blob, s)
        assert out == blob

    def test_taller_source_gets_horizontal_padding(self):
        # 2:3 → 3:4 should add horizontal padding
        blob = _make_test_jpeg(200, 300, "red")
        s = preview.CoverPreviewSettings(True, "3:4", "average", "")
        out = preview.pad_blob(blob, s)
        assert out != blob
        # Verify the output's actual aspect ratio
        from wand.image import Image as WImage
        with WImage(blob=out) as out_img:
            ratio = out_img.width / out_img.height
            assert abs(ratio - 0.75) < 0.005
            assert out_img.height == 300  # source height preserved

    def test_wider_source_gets_vertical_padding(self):
        # 2:1 → 3:4 should add vertical padding
        blob = _make_test_jpeg(400, 200, "blue")
        s = preview.CoverPreviewSettings(True, "3:4", "average", "")
        out = preview.pad_blob(blob, s)
        from wand.image import Image as WImage
        with WImage(blob=out) as out_img:
            ratio = out_img.width / out_img.height
            assert abs(ratio - 0.75) < 0.005
            assert out_img.width == 400  # source width preserved

    def test_invalid_blob_returns_input(self):
        bad = b"not a jpeg"
        s = preview.CoverPreviewSettings(True, "3:4", "average", "")
        out = preview.pad_blob(bad, s)
        assert out == bad


@pytest.mark.unit
@requires_wand
class TestFillModes:
    @pytest.mark.parametrize("mode", ["edge_mirror", "edge_blur", "average", "dominant"])
    def test_each_mode_runs_without_error(self, mode):
        blob = _make_test_jpeg(200, 300, "red")
        s = preview.CoverPreviewSettings(True, "3:4", mode, "")
        out = preview.pad_blob(blob, s)
        # Must produce different bytes than the source (padding actually happened)
        assert out != blob
        # Output is a valid JPEG
        from wand.image import Image as WImage
        with WImage(blob=out) as img:
            assert img.format.lower() in ("jpeg", "jpg")

    def test_manual_color_uses_supplied_hex(self):
        blob = _make_test_jpeg(200, 300, "red")
        s = preview.CoverPreviewSettings(True, "3:4", "manual", "#00ff00")
        out = preview.pad_blob(blob, s)
        # The padded canvas should be much wider than source (200→225 for 3:4 of h=300)
        from wand.image import Image as WImage
        with WImage(blob=out) as img:
            assert img.width == 225  # int(round(300 * 0.75))
            # Sample a pixel from the left padding strip — should be ~green
            with img.clone() as sample:
                px = sample[1, 150]  # well inside left pad
                assert px.green > 0.5
                assert px.red < 0.2
                assert px.blue < 0.2

    def test_manual_color_falls_back_to_white_on_garbage(self):
        blob = _make_test_jpeg(200, 300, "red")
        s = preview.CoverPreviewSettings(True, "3:4", "manual", "not-a-color")
        # Should not raise — just use white
        out = preview.pad_blob(blob, s)
        assert out != blob


@pytest.mark.unit
@requires_wand
class TestColorExtraction:
    def test_average_of_solid_red_is_red(self):
        from wand.image import Image
        blob = _make_test_jpeg(50, 50, "red")
        with Image(blob=blob) as img:
            hex_color = preview.average_color_hex(img)
        # JPEG can drift the exact channel slightly; just check red dominates
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        assert r > 200
        assert g < 50
        assert b < 50

    def test_dominant_of_solid_blue_is_blue(self):
        from wand.image import Image
        blob = _make_test_jpeg(50, 50, "blue")
        with Image(blob=blob) as img:
            hex_color = preview.dominant_color_hex(img)
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        assert b > 200
        assert r < 50
        assert g < 50


@pytest.mark.unit
@requires_wand
class TestPadPathToCache:
    def test_writes_padded_jpeg_to_cache(self, tmp_path):
        src = tmp_path / "cover.jpg"
        src.write_bytes(_make_test_jpeg(200, 300, "red"))
        cache = tmp_path / "cache"
        s = preview.CoverPreviewSettings(True, "3:4", "average", "")
        target = preview.pad_path_to_cache(str(src), str(cache), "out.jpg", s)
        assert target is not None
        assert (cache / "out.jpg").is_file()

    def test_cache_hit_skips_regeneration(self, tmp_path):
        src = tmp_path / "cover.jpg"
        src.write_bytes(_make_test_jpeg(200, 300, "red"))
        cache = tmp_path / "cache"
        cache.mkdir()
        # Pre-populate the cache file with sentinel bytes
        sentinel = b"already-here"
        (cache / "out.jpg").write_bytes(sentinel)
        s = preview.CoverPreviewSettings(True, "3:4", "average", "")
        target = preview.pad_path_to_cache(str(src), str(cache), "out.jpg", s)
        assert target is not None
        assert (cache / "out.jpg").read_bytes() == sentinel

    def test_disabled_returns_none(self, tmp_path):
        src = tmp_path / "cover.jpg"
        src.write_bytes(_make_test_jpeg(200, 300))
        s = preview.CoverPreviewSettings(False, "3:4", "average", "")
        target = preview.pad_path_to_cache(str(src), str(tmp_path / "cache"), "out.jpg", s)
        assert target is None

    def test_missing_source_returns_none(self, tmp_path):
        s = preview.CoverPreviewSettings(True, "3:4", "average", "")
        target = preview.pad_path_to_cache(
            str(tmp_path / "no-such.jpg"), str(tmp_path / "cache"), "out.jpg", s,
        )
        assert target is None


# ---- Preset catalog coverage --------------------------------------------


class TestPresetCatalogCoverage:
    """Three dicts (PRESET_ASPECTS, PRESET_LABELS, PRESET_GROUPS) must stay
    in lock-step. Drift causes UI dropdowns to render mismatched options or
    omit devices that the engine knows about.
    """

    def test_preset_aspects_keys_match_labels_and_groups(self):
        """Every PRESET_ASPECTS key must have a PRESET_LABELS entry and
        appear in exactly one PRESET_GROUPS bucket."""
        from cps.services import cover_preview as cp

        assert set(cp.PRESET_ASPECTS.keys()) == set(cp.PRESET_LABELS.keys()), (
            "PRESET_ASPECTS / PRESET_LABELS key sets diverged"
        )

        grouped = []
        for _group_label, keys in cp.PRESET_GROUPS:
            grouped.extend(keys)
        assert sorted(grouped) == sorted(cp.PRESET_ASPECTS.keys()), (
            "PRESET_GROUPS doesn't cover the same keys as PRESET_ASPECTS"
        )
        assert len(grouped) == len(set(grouped)), (
            "a preset appears in more than one PRESET_GROUPS bucket"
        )

    def test_default_preset_is_in_catalog(self):
        """DEFAULT_PRESET must be a valid key in PRESET_ASPECTS — otherwise
        first-run users get an empty preview."""
        from cps.services import cover_preview as cp
        assert cp.DEFAULT_PRESET in cp.PRESET_ASPECTS

    def test_every_preset_yields_a_portrait_ratio(self):
        """Every preset, parsed through parse_target_ratio, must produce a
        ratio in the plausible e-reader range [0.5, 1.0] — taller than
        wide, matching portrait orientation."""
        from cps.services import cover_preview as cp
        for key in cp.PRESET_ASPECTS:
            ratio = cp.parse_target_ratio(key)
            assert 0.5 <= ratio <= 1.0, f"{key}: ratio={ratio} out of e-reader range"

    def test_historical_kobo_presets_preserved(self):
        """The original 3 Kobo preset keys must be present forever — they
        are persisted in admin DB rows as `config_kobo_cover_padding_aspect`.
        Removing any of them breaks existing Kobo-sync setups on upgrade."""
        from cps.services import cover_preview as cp
        for key in ("kobo_libra_color", "kobo_libra_2", "kobo_clara"):
            assert key in cp.PRESET_ASPECTS, (
                f"historical kobo preset {key!r} disappeared — "
                "this would break existing admin DB configs on upgrade"
            )
