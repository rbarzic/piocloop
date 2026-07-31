"""PLAN.md parsing.

Agent-agnostic: this module knows nothing about PI or OpenCode. Ported verbatim
from pyocloop, which in turn ported it from OCLoop's src/lib/plan-parser.ts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PlanProgress:
    total: int
    completed: int
    pending: int
    manual: int
    blocked: int
    percent_complete: int


@dataclass
class TaskLine:
    type: str  # "completed" | "pending" | "manual" | "blocked" | "not-a-task"
    description: str
    blocked_reason: str = ""


def parse_task_line(line: str) -> TaskLine:
    trimmed = line.strip()

    if not trimmed.startswith("- ["):
        return TaskLine("not-a-task", "")

    close = trimmed.find("]", 3)
    if close == -1:
        return TaskLine("not-a-task", "")

    checkbox = trimmed[3:close].strip()
    after = trimmed[close + 1:].strip()

    if re.fullmatch(r"[xX]", checkbox):
        return TaskLine("completed", after)

    if re.fullmatch(r"MANUAL", checkbox, re.IGNORECASE):
        return TaskLine("manual", after)

    if checkbox == "" and after.upper().startswith("[MANUAL]"):
        desc = re.sub(r"^\[MANUAL\]\s*", "", after, flags=re.IGNORECASE)
        return TaskLine("manual", desc)

    if re.match(r"^BLOCKED", checkbox, re.IGNORECASE):
        reason = re.sub(r"^BLOCKED[:\s]*", "", checkbox, flags=re.IGNORECASE)
        return TaskLine("blocked", after, reason)

    if checkbox == "" and re.match(r"^\[BLOCKED", after, re.IGNORECASE):
        m = re.match(r"^\[BLOCKED[:\s]*([^\]]*)\]\s*(.*)", after, re.IGNORECASE)
        if m:
            return TaskLine("blocked", m.group(2) or "", (m.group(1) or "").strip())

    if checkbox == "":
        return TaskLine("pending", after)

    return TaskLine("not-a-task", "")


def parse_plan(content: str) -> PlanProgress:
    total = completed = manual = blocked = 0

    for line in content.splitlines():
        task = parse_task_line(line)
        if task.type == "not-a-task":
            continue
        total += 1
        if task.type == "completed":
            completed += 1
        elif task.type == "manual":
            manual += 1
        elif task.type == "blocked":
            blocked += 1

    pending = total - completed - manual - blocked
    denominator = total - manual
    percent = round((completed / denominator) * 100) if denominator > 0 else 100

    return PlanProgress(
        total=total,
        completed=completed,
        pending=pending,
        manual=manual,
        blocked=blocked,
        percent_complete=percent,
    )


def parse_plan_complete(content: str) -> str | None:
    matches = list(re.finditer(
        r"^<plan-complete>([\s\S]*?)<\/plan-complete>",
        content,
        re.MULTILINE,
    ))
    if not matches:
        return None
    return matches[-1].group(1).strip()


def get_current_task(content: str) -> str | None:
    for line in content.splitlines():
        task = parse_task_line(line)
        if task.type == "pending" and task.description:
            return task.description
    return None


def is_plan_complete(plan_path: Path) -> bool:
    try:
        content = plan_path.read_text(encoding="utf-8")
        return parse_plan_complete(content) is not None
    except OSError:
        return False


def read_plan_progress(plan_path: Path) -> PlanProgress:
    content = plan_path.read_text(encoding="utf-8")
    return parse_plan(content)


def read_current_task(plan_path: Path) -> str | None:
    content = plan_path.read_text(encoding="utf-8")
    return get_current_task(content)


def read_plan_complete_summary(plan_path: Path) -> str | None:
    try:
        content = plan_path.read_text(encoding="utf-8")
        return parse_plan_complete(content)
    except OSError:
        return None
