"""PluginContext rendering channel — the plugin-facing seam.

These assert the CONTRACT (event name, payload shape, never-raises) rather
than snapshotting cell values; the pixel math is covered by
tests/agent/test_image_preview.py.

The seam matters: agent/image_preview.py is tested at the data layer and
ui-tui/src/app/createGatewayEventHandler.ts at the Ink layer, but the
PluginContext methods that bridge them are what plugins actually call.
"""

from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from hermes_cli.plugins import PluginContext, PluginManifest


@pytest.fixture
def ctx():
    return PluginContext(
        manifest=PluginManifest(name="test-plugin"), manager=MagicMock()
    )


@pytest.fixture
def emitted():
    """Capture _emit calls made through the tui_gateway transport."""
    calls = []
    with patch("tui_gateway.server._emit", side_effect=lambda *a: calls.append(a)):
        yield calls


# -- render_canvas -----------------------------------------------------------


def test_render_canvas_emits_canvas_render_event(ctx, emitted):
    assert ctx.render_canvas("Title", [{"text": "hello"}]) is True

    assert len(emitted) == 1
    event, _sid, payload = emitted[0]
    assert event == "canvas.render"
    assert payload["title"] == "Title"
    assert payload["sections"] == [{"text": "hello"}]


def test_render_canvas_returns_false_without_transport(ctx):
    """No TUI attached (plain CLI, gateway, cron) degrades to False, not a raise."""
    with patch("tui_gateway.server._emit", side_effect=RuntimeError("no transport")):
        assert ctx.render_canvas("Title", []) is False


def test_render_canvas_never_raises(ctx):
    """A cosmetic surface must not abort a turn."""
    with patch("tui_gateway.server._emit", side_effect=Exception("boom")):
        assert ctx.render_canvas("t", [{"rows": [["k", "v"]]}]) is False


# -- render_image ------------------------------------------------------------


def test_render_image_emits_half_block_cell_pairs(ctx, emitted, tmp_path):
    path = tmp_path / "swatch.png"
    Image.new("RGB", (8, 8), (255, 0, 0)).save(path)

    assert ctx.render_image(str(path), max_cols=4, max_rows=2) is True

    event, _sid, payload = emitted[0]
    assert event == "canvas.image"
    assert payload["cells"], "cells must be non-empty"
    # Invariant: every cell is a [top, bottom] RGBA pair -- one U+2580 glyph,
    # so one text row carries two pixel rows.
    for row in payload["cells"]:
        for cell in row:
            assert len(cell) == 2
            assert all(len(channel) == 4 for channel in cell)


def test_render_image_respects_cell_budget(ctx, emitted, tmp_path):
    """Invariant: output never exceeds the requested cell budget."""
    path = tmp_path / "big.png"
    Image.new("RGB", (512, 512), (0, 128, 255)).save(path)

    ctx.render_image(str(path), max_cols=10, max_rows=4)

    cells = emitted[0][2]["cells"]
    assert len(cells) <= 4
    assert all(len(row) <= 10 for row in cells)


def test_render_image_falls_back_to_panel_on_undecodable(ctx, emitted, tmp_path):
    """A broken image degrades to a VISIBLE note, not silence."""
    path = tmp_path / "not-an-image.png"
    path.write_text("garbage")

    ctx.render_image(str(path))

    assert emitted, "an undecodable image must still emit something"
    assert emitted[0][0] == "canvas.render"  # panel fallback, not canvas.image


def test_render_image_missing_file_never_raises(ctx, emitted):
    assert ctx.render_image("/nonexistent/nope.png") in (True, False)


def test_render_image_carries_a_caption(ctx, emitted, tmp_path):
    """The caption is how a user identifies which image they are looking at."""
    path = tmp_path / "chart.png"
    Image.new("RGB", (16, 16), (10, 20, 30)).save(path)

    ctx.render_image(str(path), max_cols=4, max_rows=2)

    payload = emitted[0][2]
    assert "chart.png" in payload["caption"]
    assert "16x16" in payload["caption"]
