"""Tests for the terminal-outcome dispatch guard (ADO #502 Part C, #522).

These cover the DECISION logic with an injected reader. They deliberately do
NOT stand in for the acceptance proof: #522 requires a real refused dispatch
through ``dispatch_once`` against a real tracker, because a guard proven only
against a stub is exactly the defect the rule exists to catch. See the card
for the recorded live run.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_terminal_guard as tg


def _reader(status, value):
    def _r(ref, **kw):
        return status, value
    return _r


class TestParse:
    def test_parses_a_line_anchored_reference(self):
        ref = tg.parse_tracker_ref("Goal: x\nTracker: ado://Org/Proj/499\n")
        assert ref == tg.TrackerRef("Org", "Proj", 499)

    def test_prose_cannot_forge_a_link(self):
        # 513 of 1044 live cards carry a bare #nnn that is NOT a tracker ref.
        assert tg.parse_tracker_ref("see #499 and Tracker: ado://O/P/1 inline") is None

    def test_no_reference_is_not_an_error(self):
        assert tg.parse_tracker_ref("an ordinary card body") is None
        assert tg.parse_tracker_ref(None) is None
        assert tg.parse_tracker_ref("") is None

    def test_two_distinct_references_refuse_to_guess(self):
        body = "Tracker: ado://O/P/1\nTracker: ado://O/P/2\n"
        assert tg.parse_tracker_ref(body) is None

    def test_the_same_reference_twice_is_not_ambiguous(self):
        body = "Tracker: ado://O/P/7\nTracker: ado://O/P/7\n"
        assert tg.parse_tracker_ref(body) == tg.TrackerRef("O", "P", 7)


class TestCheck:
    def test_no_tracker_line_dispatches(self):
        v = tg.check("plain card", reader=_reader("ok", "completed"))
        assert v.action == "dispatch"

    @pytest.mark.parametrize("outcome", sorted(tg.TERMINAL_OUTCOMES))
    def test_each_ruled_outcome_refuses(self, outcome):
        v = tg.check("Tracker: ado://O/P/1\n", reader=_reader("ok", outcome))
        assert v.action == "refuse"
        assert outcome in v.reason
        assert "never restarts" in v.reason

    def test_unset_outcome_dispatches(self):
        v = tg.check("Tracker: ado://O/P/1\n", reader=_reader("ok", None))
        assert v.action == "dispatch"

    def test_junk_value_still_refuses_and_is_flagged(self):
        # ADO picklists are advisory over REST — a scripted write can put
        # anything in this field. Treating an unknown value as non-terminal
        # would make the guard bypassable by writing junk.
        v = tg.check("Tracker: ado://O/P/1\n", reader=_reader("ok", "banana"))
        assert v.action == "refuse"
        assert "UNRECOGNISED" in v.reason

    def test_case_is_ignored_when_matching_ruled_values(self):
        v = tg.check("Tracker: ado://O/P/1\n", reader=_reader("ok", "Completed"))
        assert v.action == "refuse"
        assert "UNRECOGNISED" not in v.reason

    def test_unreachable_tracker_defers_rather_than_accepting(self):
        v = tg.check("Tracker: ado://O/P/1\n", reader=_reader("unreachable", "boom"))
        assert v.action == "defer"

    def test_whitespace_only_value_is_treated_as_unset(self):
        v = tg.check("Tracker: ado://O/P/1\n", reader=_reader("ok", "   "))
        assert v.action == "dispatch"
