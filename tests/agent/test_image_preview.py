"""Behavior contracts for agent.image_preview.

Asserts invariants (aspect preservation, bounds, graceful failure), not frozen
byte counts — encoder output size is an implementation detail that will drift.
"""

import struct
import zlib

import pytest

from agent import image_preview as ip


def _png(path, w, h, rgb=(200, 120, 60)):
    """Write a minimal valid PNG without requiring PIL to build it."""

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    return path


class TestFitCells:
    def test_square_image_fills_the_box(self):
        cols, rows = ip.fit_cells(100, 100, max_cols=40, max_rows=20)
        assert (cols, rows) == (40, 20)

    def test_never_exceeds_the_cell_box(self):
        for w, h in [(1, 5000), (5000, 1), (1920, 1080), (7, 13)]:
            cols, rows = ip.fit_cells(w, h, max_cols=40, max_rows=20)
            assert 1 <= cols <= 40, (w, h, cols)
            assert 1 <= rows <= 20, (w, h, rows)

    # NOTE: cell COUNTS are not directly comparable — a terminal cell is ~2x
    # taller than wide, so a portrait image legitimately returns more columns
    # than rows. These assert the ON-SCREEN aspect ratio instead, which is the
    # property users actually perceive.
    @staticmethod
    def _screen_aspect(cols, rows, cell_w=8, cell_h=16):
        return (rows * cell_h) / (cols * cell_w)

    def test_wide_image_renders_wide_on_screen(self):
        cols, rows = ip.fit_cells(1920, 1080, max_cols=40, max_rows=40)
        assert self._screen_aspect(cols, rows) < 1

    def test_tall_image_renders_tall_on_screen(self):
        cols, rows = ip.fit_cells(1080, 1920, max_cols=40, max_rows=40)
        assert self._screen_aspect(cols, rows) > 1

    def test_screen_aspect_tracks_source_aspect(self):
        for w, h in [(1920, 1080), (1080, 1920), (100, 100), (800, 600)]:
            cols, rows = ip.fit_cells(w, h, max_cols=40, max_rows=40)
            got = self._screen_aspect(cols, rows)
            want = h / w
            # Cell quantization makes this approximate, not exact.
            assert abs(got - want) / want < 0.15, (w, h, got, want)

    def test_degenerate_dimensions_do_not_crash(self):
        assert ip.fit_cells(0, 0, max_cols=40, max_rows=20) == (1, 1)
        assert ip.fit_cells(-5, 10, max_cols=40, max_rows=20) == (1, 1)


class TestGuards:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ip.ThumbnailError):
            ip.render_thumbnail(tmp_path / "nope.png", mode="unicode")

    def test_directory_raises(self, tmp_path):
        with pytest.raises(ip.ThumbnailError):
            ip.render_thumbnail(tmp_path, mode="unicode")

    def test_non_image_raises(self, tmp_path):
        p = tmp_path / "notes.txt"
        p.write_text("definitely not a png")
        with pytest.raises(ip.ThumbnailError):
            ip.render_thumbnail(p, mode="unicode")

    def test_empty_file_raises(self, tmp_path):
        p = tmp_path / "empty.png"
        p.write_bytes(b"")
        with pytest.raises(ip.ThumbnailError):
            ip.render_thumbnail(p, mode="unicode")

    def test_oversized_file_rejected_before_decode(self, tmp_path, monkeypatch):
        p = _png(tmp_path / "big.png", 4, 4)
        monkeypatch.setattr(ip, "MAX_SOURCE_BYTES", 1)
        with pytest.raises(ip.ThumbnailError):
            ip.render_thumbnail(p, mode="unicode")

    def test_off_mode_returns_empty_without_touching_disk(self):
        # Must not raise even though the path does not exist.
        assert ip.render_thumbnail("/nonexistent/x.png", mode="off") == ""


class TestRender:
    def test_unicode_mode_produces_output(self, tmp_path):
        p = _png(tmp_path / "a.png", 32, 32)
        out = ip.render_thumbnail(p, mode="unicode", max_cols=20, max_rows=10)
        assert out
        assert "\x1b[" in out  # carries color

    def test_unicode_respects_column_bound(self, tmp_path):
        p = _png(tmp_path / "a.png", 64, 64)
        out = ip.render_thumbnail(p, mode="unicode", max_cols=12, max_rows=40)
        widest = max(len(line) for line in _strip(out).split("\n"))
        assert widest <= 12

    def test_graphics_modes_emit_their_protocol_marker(self, tmp_path):
        p = _png(tmp_path / "a.png", 32, 32)
        assert ip.render_thumbnail(p, mode="kitty").startswith("\x1b_G")
        assert "\x1b]1337;File=" in ip.render_thumbnail(p, mode="iterm")

    def test_cells_api_shape_matches_requested_box(self, tmp_path):
        p = _png(tmp_path / "a.png", 40, 40)
        grid = ip.thumbnail_cells(p, max_cols=16, max_rows=8)
        assert grid and len(grid[0]) <= 16
        top, bottom = grid[0][0]
        assert len(top) == 4 and len(bottom) == 4  # RGBA pairs


class TestDescribe:
    def test_reports_dimensions(self, tmp_path):
        p = _png(tmp_path / "a.png", 21, 9)
        info = ip.describe(p)
        assert info["width"] == 21 and info["height"] == 9
        assert info["name"] == "a.png"

    def test_never_raises_on_bad_input(self, tmp_path):
        info = ip.describe(tmp_path / "missing.png")
        assert "error" in info and info["name"] == "missing.png"


class TestIsImagePath:
    def test_recognizes_common_suffixes(self):
        assert ip.is_image_path("/x/y.PNG")
        assert ip.is_image_path("a.jpeg")
        assert not ip.is_image_path("a.txt")
        assert not ip.is_image_path("a")


def _strip(s):
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", s)
