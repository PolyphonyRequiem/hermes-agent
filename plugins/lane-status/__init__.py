"""lane-status: render live lane/herdr-session state as a TUI panel.

The concrete consumer for the ``canvas.render`` event. Reads the lane index
and live herdr session list, then emits a structured panel that the Ink UI
draws with its own <Panel> component — theme-aware, width-aware, no ANSI.
"""

import json
import os
import pwd
import subprocess

_CTX = {}


def _login_home():
    """Login home, NOT $HOME: under a Hermes profile $HOME is the profile home."""
    return pwd.getpwuid(os.getuid()).pw_dir


def _lanes():
    path = os.path.join(_login_home(), ".hermes", "lanes.json")
    try:
        with open(path) as fh:
            return json.loads(fh.read().replace("${LOGIN_HOME}", _login_home()))
    except Exception:
        return None


def _sessions():
    try:
        out = subprocess.run(
            ["herdr", "session", "list", "--json"],
            capture_output=True, text=True, timeout=8,
            env={**os.environ, "HOME": _login_home()},
        )
        return {s["name"]: s for s in json.loads(out.stdout).get("sessions", [])}
    except Exception:
        return {}


def build_sections():
    """Return PanelSection dicts describing current lane state."""
    idx = _lanes()
    if not idx:
        return [{"text": "lanes.json not found — no lane index on this host."}]

    live = _sessions()
    sections = []
    up = 0

    for name, lane in idx.get("lanes", {}).items():
        sess = lane.get("herdr_session", "?")
        info = live.get(sess)
        running = bool(info and info.get("running"))
        up += 1 if running else 0

        rows = [
            ["session", sess],
            ["state", "up" if running else "down"],
            ["tenants", ", ".join(lane.get("tenant_prefixes", [])) or "—"],
        ]
        excl = lane.get("exclusive_resources") or []
        if excl:
            rows.append(["exclusive", ", ".join(excl)])

        sections.append({"rows": rows, "title": name})

    total = len(idx.get("lanes", {}))
    sections.append({"text": "{}/{} lanes up · rendered by plugin, not the model".format(up, total)})
    return sections


def render(ctx=None):
    ctx = ctx or _CTX.get("ctx")
    if ctx is None:
        return False
    return ctx.render_canvas("LANE STATUS", build_sections())


def _on_post_tool_call(tool_name=None, **kwargs):
    if tool_name == "kanban_heartbeat":
        render()
    return None


def register(ctx):
    _CTX["ctx"] = ctx
    ctx.register_hook("post_tool_call", _on_post_tool_call)
