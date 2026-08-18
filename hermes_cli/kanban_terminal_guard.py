"""Refuse to dispatch a run card whose tracker item already ended.

Ruled by ADO ``PolyphonyRequiem/Hyperbright/502`` Part C (2026-08-16) and
built by #522.

The rule, in one sentence: **a terminal work item never restarts.**
``completed``, ``canceled``, ``rejected`` and ``failed`` are final; a retry
is a NEW work item referencing the original, never a resurrection. That
structurally kills the retry-storm failure mode, because there is no
resurrect-and-retry path to loop on.

🔴 **Why the check lives here and not on the tracker.** Azure DevOps cannot
enforce immutability. Nothing stops a human or an agent moving ``Done`` back
to ``Doing``, and the process carries no state that would prevent it (every
work item type is To do / Doing / Done). So the record and the guard are
deliberately different systems:

    ADO is the RECORD.  The dispatcher is the GUARD.

Claiming ADO enforces this would be exactly the overstated-maturity failure
#502 Part B rules against.

Scope, stated so nobody widens it by accident:

* Only cards carrying a line-anchored ``Tracker: ado://<org>/<project>/<id>``
  are considered. A card without one is NOT refused here — the fail-closed
  rule for a missing tracker line is a separate, unbuilt decision, and
  conflating them would silently stop every non-Hyperbright board on the
  host.
* An unreachable tracker DEFERS the spawn rather than refusing it. The card
  is retried on the next tick. Refusing on a network blip would convert a
  transient outage into a board-wide halt; accepting on a network blip would
  make the guard advisory, which is the decay this rule exists to prevent.
  Deferring is the only option that is neither.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass

#: ``Tracker: ado://PolyphonyRequiem/Hyperbright/499`` on its own line.
#: Anchored to line start so prose mentioning a card cannot forge a link --
#: that mattered: 513 of 1044 existing cards contain a bare ``#nnn`` in their
#: body or title, none of which are tracker references.
#:
#: Kept byte-identical to ``correspondence.py`` in hyperbright-core, which is
#: the tested original. If one changes, change both.
_TRACKER_LINE = re.compile(
    r"^[ \t]*Tracker:[ \t]*ado://(?P<org>[^/\s]+)/(?P<project>[^/\s]+)/(?P<item>\d+)[ \t]*$",
    re.MULTILINE,
)

#: The ADO resource id ``az rest`` authenticates against.
_ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"

#: The four terminal outcomes ruled by #502 Part C. A value outside this set
#: is still treated as terminal -- see :func:`check` for why.
TERMINAL_OUTCOMES = frozenset({"completed", "canceled", "rejected", "failed"})

FIELD = "Custom.TerminalOutcome"


@dataclass(frozen=True)
class TrackerRef:
    """A resolved pointer to one work item on one tracker."""

    org: str
    project: str
    item: int

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"ado://{self.org}/{self.project}/{self.item}"


@dataclass(frozen=True)
class GuardVerdict:
    """What the dispatcher should do with this card.

    ``action`` is one of:

    ``"dispatch"``
        No tracker reference, or the referenced item has no terminal
        outcome. Proceed normally.
    ``"refuse"``
        The referenced item is terminal. Do not spawn, now or ever --
        a retry must be a new work item.
    ``"defer"``
        The tracker could not be read. Try again next tick.
    """

    action: str
    reason: str = ""
    ref: TrackerRef | None = None
    outcome: str | None = None


def parse_tracker_ref(body: str | None) -> TrackerRef | None:
    """Extract the tracker reference from a kanban card body.

    Returns ``None`` when the card carries no reference (the common case --
    it is not an error) and ALSO when it carries more than one distinct
    reference, because guessing which one is meant is exactly the silent
    wrongness this design forbids.
    """
    if not body:
        return None
    found = {
        TrackerRef(m.group("org"), m.group("project"), int(m.group("item")))
        for m in _TRACKER_LINE.finditer(body)
    }
    if len(found) != 1:
        return None
    return found.pop()


def read_terminal_outcome(ref: TrackerRef, *, timeout: int = 120) -> tuple[str, str | None]:
    """Read ``Custom.TerminalOutcome`` off one ADO work item.

    Returns ``(status, value)`` where status is ``"ok"`` or ``"unreachable"``.
    A work item that exists but has never had the field set comes back as
    ``("ok", None)`` -- ADO omits unset fields entirely rather than
    returning null, so absence from the payload IS the unset signal.

    ``az`` intermittently hangs at startup, hence the hard timeout.
    """
    url = (
        f"https://dev.azure.com/{ref.org}/{ref.project}"
        f"/_apis/wit/workitems/{ref.item}?api-version=7.1"
    )
    env = dict(os.environ)
    env.setdefault("AZURE_CONFIG_DIR", "/home/polyphonyrequiem/.azure")
    try:
        proc = subprocess.run(
            [
                "az", "rest",
                "--resource", _ADO_RESOURCE,
                "--url", url,
                "-o", "json",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "unreachable", f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return "unreachable", (proc.stderr or "").strip()[:400]
    try:
        payload = json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        return "unreachable", f"unparseable response: {exc}"
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        return "unreachable", "response carried no fields object"
    raw = fields.get(FIELD)
    if raw is None:
        return "ok", None
    value = str(raw).strip()
    return "ok", (value or None)


def check(
    body: str | None,
    *,
    reader=read_terminal_outcome,
) -> GuardVerdict:
    """Decide whether a card may be dispatched.

    ``reader`` is injected so the decision logic can be tested without a
    network, but the DISPATCHER always uses the real one -- a guard proven
    only against a stub is the defect this rule exists to catch.
    """
    ref = parse_tracker_ref(body)
    if ref is None:
        return GuardVerdict("dispatch", "no tracker reference")
    status, value = reader(ref)
    if status != "ok":
        return GuardVerdict(
            "defer",
            f"tracker {ref} unreachable: {value}",
            ref=ref,
        )
    # Normalise here as well as in the reader: `check` must not depend on a
    # caller-supplied reader having done it, or the guard's behaviour varies
    # by who called it.
    value = (value or "").strip() or None
    if value is None:
        return GuardVerdict("dispatch", f"tracker {ref} is not terminal", ref=ref)
    # 🔴 Any non-empty value refuses, not just the four ruled ones. ADO
    # picklists are advisory over REST: a scripted write can put `banana`
    # into this field and ADO returns 200. Treating an unrecognised value as
    # non-terminal would make the guard bypassable by writing junk -- so an
    # unrecognised value is refused AND flagged.
    known = value.lower() in TERMINAL_OUTCOMES
    detail = "" if known else " (UNRECOGNISED value — not one of the four ruled outcomes)"
    return GuardVerdict(
        "refuse",
        (
            f"REFUSED: tracker item {ref} is terminal "
            f"({FIELD}={value!r}){detail}. "
            "A terminal work item never restarts — file a NEW work item "
            "referencing the original. Ruled by ADO #502 Part C."
        ),
        ref=ref,
        outcome=value,
    )
