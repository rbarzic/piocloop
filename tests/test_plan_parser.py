"""Tests for plan_parser — Phase 1 of PLAN.md."""

from __future__ import annotations

import textwrap

import pytest

from piocloop.plan_parser import (
    get_current_task,
    parse_plan,
    parse_plan_complete,
    parse_task_line,
)


def dedent(s: str) -> str:
    return textwrap.dedent(s).lstrip("\n")


# ---------------------------------------------------------------------------
# parse_task_line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,expected_type,expected_desc",
    [
        ("- [ ] do a thing", "pending", "do a thing"),
        ("- [x] done thing", "completed", "done thing"),
        ("- [X] done upper", "completed", "done upper"),
        ("  - [ ] indented", "pending", "indented"),
        ("- [MANUAL] human work", "manual", "human work"),
        ("- [manual] lower", "manual", "lower"),
        ("- [ ] [MANUAL] inline form", "manual", "inline form"),
        ("## Phase 1", "not-a-task", ""),
        ("", "not-a-task", ""),
        ("- not a checkbox", "not-a-task", ""),
        ("- [unterminated", "not-a-task", ""),
    ],
)
def test_parse_task_line_types(line, expected_type, expected_desc):
    task = parse_task_line(line)
    assert task.type == expected_type
    assert task.description == expected_desc


def test_parse_task_line_blocked_in_checkbox():
    task = parse_task_line("- [BLOCKED: needs credentials] deploy to prod")
    assert task.type == "blocked"
    assert task.description == "deploy to prod"
    assert task.blocked_reason == "needs credentials"


def test_parse_task_line_blocked_inline():
    task = parse_task_line("- [ ] [BLOCKED: no API key] call the service")
    assert task.type == "blocked"
    assert task.description == "call the service"
    assert task.blocked_reason == "no API key"


# ---------------------------------------------------------------------------
# parse_plan
# ---------------------------------------------------------------------------


def test_parse_plan_counts():
    progress = parse_plan(
        dedent(
            """
            # Plan
            ## Phase 1
            - [x] one
            - [x] two
            - [ ] three
            - [MANUAL] four
            - [BLOCKED: nope] five
            """
        )
    )
    assert progress.total == 5
    assert progress.completed == 2
    assert progress.pending == 1
    assert progress.manual == 1
    assert progress.blocked == 1


def test_parse_plan_percent_excludes_manual_only():
    """Manual tasks leave the denominator; blocked ones do not.

    Documented consequence: a plan whose only remaining work is blocked never
    reaches 100%. Asserted here so the behaviour is a decision, not an accident.
    """
    progress = parse_plan(
        dedent(
            """
            - [x] one
            - [MANUAL] two
            - [BLOCKED: nope] three
            """
        )
    )
    # denominator = total(3) - manual(1) = 2, completed = 1
    assert progress.percent_complete == 50


def test_parse_plan_empty():
    progress = parse_plan("# Just a heading\n\nSome prose.\n")
    assert progress.total == 0
    assert progress.percent_complete == 100


def test_parse_plan_all_done():
    progress = parse_plan("- [x] one\n- [x] two\n")
    assert progress.percent_complete == 100


# ---------------------------------------------------------------------------
# parse_plan_complete
# ---------------------------------------------------------------------------


def test_parse_plan_complete_absent():
    assert parse_plan_complete("- [ ] work to do\n") is None


def test_parse_plan_complete_extracts_summary():
    content = "- [x] one\n<plan-complete>All done.</plan-complete>\n"
    assert parse_plan_complete(content) == "All done."


def test_parse_plan_complete_multiline():
    content = "<plan-complete>\nline one\nline two\n</plan-complete>\n"
    assert parse_plan_complete(content) == "line one\nline two"


def test_parse_plan_complete_takes_last_match():
    """A resumed plan can accumulate several markers; the newest one wins."""
    content = (
        "<plan-complete>first run</plan-complete>\n"
        "- [x] more work\n"
        "<plan-complete>second run</plan-complete>\n"
    )
    assert parse_plan_complete(content) == "second run"


def test_parse_plan_complete_requires_line_start():
    """Indented/inline markers are ignored, so prose about the tag can't trip it."""
    assert parse_plan_complete("  <plan-complete>nope</plan-complete>\n") is None


# ---------------------------------------------------------------------------
# get_current_task
# ---------------------------------------------------------------------------


def test_get_current_task_first_pending():
    content = dedent(
        """
        - [x] done
        - [BLOCKED: nope] skipped
        - [ ] the next one
        - [ ] a later one
        """
    )
    assert get_current_task(content) == "the next one"


def test_get_current_task_none_when_nothing_pending():
    assert get_current_task("- [x] done\n- [MANUAL] human\n") is None
