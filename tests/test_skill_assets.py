"""Tests for the shipped skill in skills/piocloop-setup.

The skill documents piocloop's contract. If the code changes and the skill does
not, agents get confidently wrong instructions — so the claims that can be
checked mechanically are checked here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from piocloop.cli import _PLAN_TEMPLATE, _PROMPT_TEMPLATE
from piocloop.plan_parser import parse_plan, parse_plan_complete

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "piocloop-setup"
SKILL_MD = SKILL_DIR / "SKILL.md"
PLAN_ASSET = SKILL_DIR / "assets" / "PLAN.template.md"
PROMPT_ASSET = SKILL_DIR / "assets" / "loop-prompt.template.md"


def frontmatter() -> dict[str, str]:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    block = text.split("---\n", 2)[1]
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if line and not line.startswith((" ", "-")) and ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


# ---------------------------------------------------------------------------
# Layout and frontmatter (Agent Skills standard)
# ---------------------------------------------------------------------------


def test_expected_files_exist():
    for rel in (
        "SKILL.md",
        "references/PLAN-FORMAT.md",
        "references/PROMPT-AUTHORING.md",
        "assets/PLAN.template.md",
        "assets/loop-prompt.template.md",
    ):
        assert (SKILL_DIR / rel).is_file(), f"missing {rel}"


def test_required_frontmatter_fields():
    fields = frontmatter()
    assert fields["name"] == "piocloop-setup"
    assert fields["description"]


def test_name_obeys_the_standard():
    name = frontmatter()["name"]
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name), name
    assert len(name) <= 64
    # The standard wants name == parent directory; pi is lenient, others are not.
    assert name == SKILL_DIR.name


def test_description_within_limit_and_mentions_triggers():
    description = frontmatter()["description"]
    assert len(description) <= 1024
    for trigger in ("piocloop", "piloop", "PLAN.md"):
        assert trigger in description


# ---------------------------------------------------------------------------
# The templates must actually work with piocloop
# ---------------------------------------------------------------------------


def test_plan_template_parses_as_a_plan():
    progress = parse_plan(PLAN_ASSET.read_text(encoding="utf-8"))
    assert progress.total == 12
    assert progress.pending == 12
    assert progress.completed == 0


def test_plan_template_comment_is_not_parsed_as_tasks():
    """Regression: the syntax legend used to be counted as real tasks.

    Any line starting with "- [" is a task to the parser, even inside an HTML
    comment — so a legend written as "- [ ] pending" left a phantom task that
    the loop would hand to the agent once the real work was done. (The same
    defect is still present in pyocloop's template.)
    """
    from piocloop.plan_parser import get_current_task

    text = PLAN_ASSET.read_text(encoding="utf-8")
    all_done = text.replace("- [ ] **", "- [x] **")
    assert get_current_task(all_done) is None, "template leaves a phantom task"


def test_plan_template_is_not_accidentally_complete():
    assert parse_plan_complete(PLAN_ASSET.read_text(encoding="utf-8")) is None


def test_prompt_template_has_the_placeholder():
    assert "{{PLAN_FILE}}" in PROMPT_ASSET.read_text(encoding="utf-8")


def test_prompt_template_has_no_frontmatter():
    """piocloop sends this file verbatim; a YAML header would become prompt text.

    This is the concrete difference from pyocloop, whose template required
    `description: Execute loop` for OpenCode.
    """
    assert not PROMPT_ASSET.read_text(encoding="utf-8").startswith("---")


def test_prompt_template_marks_progress_and_completion():
    text = PROMPT_ASSET.read_text(encoding="utf-8")
    assert "[x]" in text, "must tell the agent to tick tasks (stall detection)"
    assert "<plan-complete>" in text, "must tell the agent how to end the run"


# ---------------------------------------------------------------------------
# The skill must agree with the CLI it documents
# ---------------------------------------------------------------------------


def test_bootstrap_templates_share_the_skill_contract():
    """`piloop bootstrap` and the skill must not teach different rules."""
    assert "{{PLAN_FILE}}" in _PROMPT_TEMPLATE
    assert not _PROMPT_TEMPLATE.startswith("---")
    assert "<plan-complete>" in _PROMPT_TEMPLATE
    assert parse_plan(_PLAN_TEMPLATE).pending == 3


@pytest.mark.parametrize(
    "claim",
    [
        "piloop run",
        "piloop bootstrap",
        "piloop doctor",
        "pi --list-models",
        "--max-stalls",
        "--dialog-policy",
    ],
)
def test_skill_documents_real_commands(claim: str):
    assert claim in SKILL_MD.read_text(encoding="utf-8")


def test_skill_does_not_mention_the_opencode_predecessor_commands():
    """Guards against a half-finished port leaving `ocloop`/`opencode` behind."""
    text = SKILL_MD.read_text(encoding="utf-8")
    for stale in (r"(?<!pi)ocloop run", r"opencode models", r"OPENCODE"):
        assert re.search(stale, text) is None, stale


def test_column_zero_rule_is_documented_correctly():
    """The tag must start a line — verify the doc matches the parser."""
    assert parse_plan_complete("  <plan-complete>x</plan-complete>") is None
    assert parse_plan_complete("<plan-complete>x</plan-complete>") == "x"
    text = (SKILL_DIR / "references" / "PLAN-FORMAT.md").read_text(encoding="utf-8")
    assert "beginning of a line" in text
