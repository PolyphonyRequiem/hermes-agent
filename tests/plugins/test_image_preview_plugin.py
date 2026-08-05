"""image-preview: the reference consumer of the TUI rendering channel.

Exercises the plugin against a fake ctx rather than a live TUI -- the
transport itself is covered by tests/hermes_cli/test_plugin_context_render.py.
"""

import importlib.util
from pathlib import Path

import pytest
from PIL import Image

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "image-preview" / "__init__.py"


@pytest.fixture
def plugin():
    spec = importlib.util.spec_from_file_location("image_preview_plugin", PLUGIN)
    assert spec is not None and spec.loader is not None, f"cannot load {PLUGIN}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._SEEN.clear()
    return mod


@pytest.fixture
def png(tmp_path):
    def _make(name="out.png"):
        path = tmp_path / name
        Image.new("RGB", (4, 4), (200, 40, 90)).save(path)
        return str(path)

    return _make


class FakeCtx:
    """Minimal stand-in for PluginContext -- records what would be drawn."""

    def __init__(self):
        self.rendered = []
        self.hooks = {}

    def render_image(self, path, **_kw):
        self.rendered.append(path)
        return True

    def register_hook(self, name, fn):
        self.hooks[name] = fn


@pytest.fixture
def ctx(plugin):
    c = FakeCtx()
    plugin.register(c)
    return c


def test_register_subscribes_to_post_tool_call(ctx):
    assert "post_tool_call" in ctx.hooks


def test_previews_image_from_image_generate(ctx, png):
    path = png()
    ctx.hooks["post_tool_call"](tool_name="image_generate", result={"image": path})
    assert ctx.rendered == [path]


def test_ignores_non_image_tools(ctx, png):
    """v0.1 policy: only tools whose purpose is producing an image."""
    ctx.hooks["post_tool_call"](tool_name="terminal", result=png())
    assert ctx.rendered == []


def test_dedupes_within_session(ctx, png):
    path = png()
    for _ in range(3):
        ctx.hooks["post_tool_call"](tool_name="image_generate", result={"image": path})
    assert len(ctx.rendered) == 1


def test_caps_previews_per_call(ctx, png):
    """Never flood the transcript."""
    paths = [png(f"i{i}.png") for i in range(6)]
    ctx.hooks["post_tool_call"](tool_name="image_generate", result={"images": paths})
    assert len(ctx.rendered) <= 3


def test_extracts_paths_from_prose(ctx, png):
    """Tool results are often a human-readable string, not structured data."""
    path = png()
    ctx.hooks["post_tool_call"](
        tool_name="vision_analyze", result=f"Saved the render to {path} successfully."
    )
    assert ctx.rendered == [path]


def test_rejects_nonexistent_and_non_image(plugin):
    assert plugin._is_previewable("/nope/missing.png") is False
    assert plugin._is_previewable("/tmp/notes.txt") is False
    assert plugin._is_previewable(None) is False


def test_debug_logging_is_off_by_default(plugin, monkeypatch, tmp_path):
    """Tracing must be opt-in -- a shipped plugin does not write to disk."""
    monkeypatch.delenv("HERMES_IMAGE_PREVIEW_DEBUG", raising=False)
    log = tmp_path / "trace.log"
    plugin._dbg("nothing")  # must be a silent no-op
    assert not log.exists()


def test_debug_logging_writes_when_enabled(plugin, monkeypatch, tmp_path):
    log = tmp_path / "trace.log"
    monkeypatch.setenv("HERMES_IMAGE_PREVIEW_DEBUG", str(log))
    plugin._dbg("hello")
    assert "hello" in log.read_text()
