"""image-preview: inline half-block image thumbnails in the Hermes TUI.

The concrete consumer for the ``canvas.image`` event. Turns image paths into
structured cell data that the Ink UI draws with its own <ImageCanvas>.

Why structure and not escape bytes: under ``--tui`` the agent's stdout is a
JSON-RPC pipe to the Node renderer, not a terminal, so raw kitty/sixel escapes
corrupt the transport instead of drawing. Ink also measures every cell it
renders, so an escape sequence has no countable width and is clobbered on the
next repaint. See skill ``hermes-tui-render-path``.

Scope (deliberately narrow for v0.1): previews images produced by
``image_generate``. Broad auto-detection across all tool output is a separate
policy decision -- see PREVIEW_ALL_TOOLS below.
"""

import os
import re

_CTX = {}

# Opt-in tracing for diagnosing a silent no-op (the usual cause is the plugin
# not being on the config.yaml plugins.enabled allow-list, so the hook never
# fires at all). Set HERMES_IMAGE_PREVIEW_DEBUG=/path/to/log to enable.
def _dbg(msg):
    """Append a diagnostic line when tracing is on. Best-effort; never raises."""
    dest = os.environ.get("HERMES_IMAGE_PREVIEW_DEBUG")
    if not dest:
        return
    try:
        import time

        with open(dest, "a") as fh:
            fh.write("{} {}\n".format(time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


# Suffixes we will attempt to decode. image_preview enforces its own size cap
# (64 MB) and returns an error dict rather than raising on garbage.
_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".avif")

# v0.1 policy: only preview images from tools whose entire purpose is to
# produce an image. Widening this to write_file/terminal means previewing
# anything anyone happens to write, which is noisy and surprising -- that is
# the step-2 auto-detect decision, not a default.
_IMAGE_TOOLS = {"image_generate", "vision_analyze"}
PREVIEW_ALL_TOOLS = False

# Don't re-preview the same path repeatedly within a session.
_SEEN = set()

# Bounded so a long session can't grow this without limit.
_SEEN_MAX = 256

_PATH_RE = re.compile(r"(/[^\s\"'<>|]+\.(?:png|jpe?g|gif|webp|bmp|tiff|avif))", re.I)


def _is_previewable(path):
    """True when *path* is a plausible, existing, not-yet-previewed image."""
    if not path or not isinstance(path, str):
        return False
    if not path.lower().endswith(_SUFFIXES):
        return False
    if path in _SEEN:
        return False
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def _remember(path):
    if len(_SEEN) >= _SEEN_MAX:
        _SEEN.clear()
    _SEEN.add(path)


def preview(path, title="", ctx=None, max_cols=48, max_rows=16):
    """Render *path* as an inline thumbnail. Returns True when emitted.

    Public entry point -- callable manually to smoke-test the wire:
        from plugins import image_preview  # or via the plugin registry
    """
    ctx = ctx or _CTX.get("ctx")
    if ctx is None:
        return False
    if not _is_previewable(path):
        return False
    _remember(path)
    return ctx.render_image(
        path, title=title or os.path.basename(path), max_cols=max_cols, max_rows=max_rows
    )


def _paths_from(value, depth=0):
    """Pull plausible image paths out of a tool result of unknown shape."""
    if depth > 3:
        return []
    out = []
    if isinstance(value, str):
        out.extend(_PATH_RE.findall(value))
    elif isinstance(value, dict):
        for key in ("image", "path", "image_path", "agent_visible_image", "output_path"):
            v = value.get(key)
            if isinstance(v, str):
                out.append(v)
        for v in value.values():
            out.extend(_paths_from(v, depth + 1))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(_paths_from(v, depth + 1))
    return out


def _on_post_tool_call(tool_name="", args=None, result=None, **_):
    _dbg("HOOK FIRED tool={}".format(tool_name))

    if not PREVIEW_ALL_TOOLS and tool_name not in _IMAGE_TOOLS:
        return None

    seen_here = []
    for cand in _paths_from(result) + _paths_from(args):
        if cand not in seen_here:
            seen_here.append(cand)

    _dbg("  candidates={}".format(seen_here))

    for path in seen_here[:3]:  # cap: never flood the transcript
        ok = preview(path)
        _dbg("  preview({}) -> {}".format(path, ok))

    return None


def register(ctx):
    _CTX["ctx"] = ctx
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    _dbg("REGISTERED ctx={} has_render_image={}".format(
        type(ctx).__name__, hasattr(ctx, "render_image")
    ))

