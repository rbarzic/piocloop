"""Tests for EventMapper — Phase 3 of PLAN.md.

Fixtures mirror shapes verified against pi 0.82.1.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from piocloop.pi_client import (
    EVENT_DIALOG_ANSWERED,
    EVENT_PROCESS_EXITED,
    EVENT_PROTOCOL_ERROR,
    EVENT_STDERR,
)
from piocloop.pi_events import (
    AssistantText,
    EventMapper,
    Notice,
    PlanTouched,
    ProcessExited,
    Settled,
    Started,
    Thinking,
    ToolEnded,
    ToolStarted,
    Usage,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def plan_file(tmp_path: Path) -> Path:
    p = tmp_path / "PLAN.md"
    p.write_text("- [ ] task\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Settle semantics — the single most important mapping
# ---------------------------------------------------------------------------


def test_agent_settled_produces_settled():
    assert EventMapper().map({"type": "agent_settled"}) == [Settled()]


def test_agent_end_does_not_settle():
    """agent_end may be followed by retry/compaction/queued work."""
    assert EventMapper().map({"type": "agent_end", "willRetry": False}) == []


def test_agent_end_with_retry_is_a_notice_not_a_settle():
    events = EventMapper().map({"type": "agent_end", "willRetry": True})
    assert events == [Notice("retry", "agent run failed — retrying automatically")]


def test_agent_start():
    assert EventMapper().map({"type": "agent_start"}) == [Started()]


# ---------------------------------------------------------------------------
# Tools and plan detection
# ---------------------------------------------------------------------------


def test_tool_start_extracts_path_and_detail():
    events = EventMapper().map({
        "type": "tool_execution_start",
        "toolCallId": "c1",
        "toolName": "read",
        "args": {"path": "/tmp/x.txt"},
    })
    assert events == [ToolStarted("read", "/tmp/x.txt", "/tmp/x.txt")]


def test_bash_detail_is_the_command():
    events = EventMapper().map({
        "type": "tool_execution_start",
        "toolCallId": "c1",
        "toolName": "bash",
        "args": {"command": "ls -la"},
    })
    assert events[0].detail == "ls -la"
    assert events[0].path is None


def test_tool_end_recovers_path_by_call_id(plan_file: Path):
    """tool_execution_end carries no args, so the path must be correlated."""
    mapper = EventMapper(plan_file)
    mapper.map({
        "type": "tool_execution_start",
        "toolCallId": "c1",
        "toolName": "write",
        "args": {"path": str(plan_file), "content": "..."},
    })
    events = mapper.map({
        "type": "tool_execution_end",
        "toolCallId": "c1",
        "toolName": "write",
        "args": None,
        "isError": False,
    })
    assert ToolEnded("write", str(plan_file), False) in events
    assert PlanTouched(str(plan_file)) in events


def test_reading_the_plan_is_not_a_touch(plan_file: Path):
    mapper = EventMapper(plan_file)
    mapper.map({
        "type": "tool_execution_start", "toolCallId": "c1",
        "toolName": "read", "args": {"path": str(plan_file)},
    })
    events = mapper.map({
        "type": "tool_execution_end", "toolCallId": "c1",
        "toolName": "read", "isError": False,
    })
    assert not any(isinstance(e, PlanTouched) for e in events)


def test_failed_write_to_plan_is_not_a_touch(plan_file: Path):
    mapper = EventMapper(plan_file)
    mapper.map({
        "type": "tool_execution_start", "toolCallId": "c1",
        "toolName": "write", "args": {"path": str(plan_file)},
    })
    events = mapper.map({
        "type": "tool_execution_end", "toolCallId": "c1",
        "toolName": "write", "isError": True,
    })
    assert not any(isinstance(e, PlanTouched) for e in events)


def test_writing_another_file_is_not_a_touch(plan_file: Path, tmp_path: Path):
    other = tmp_path / "src.py"
    mapper = EventMapper(plan_file)
    mapper.map({
        "type": "tool_execution_start", "toolCallId": "c1",
        "toolName": "write", "args": {"path": str(other)},
    })
    events = mapper.map({
        "type": "tool_execution_end", "toolCallId": "c1",
        "toolName": "write", "isError": False,
    })
    assert not any(isinstance(e, PlanTouched) for e in events)


def test_plan_touch_matches_through_a_relative_path(plan_file: Path, monkeypatch):
    """The agent may pass a relative path; resolution must still match."""
    monkeypatch.chdir(plan_file.parent)
    mapper = EventMapper(plan_file)
    mapper.map({
        "type": "tool_execution_start", "toolCallId": "c1",
        "toolName": "edit", "args": {"path": "PLAN.md"},
    })
    events = mapper.map({
        "type": "tool_execution_end", "toolCallId": "c1",
        "toolName": "edit", "isError": False,
    })
    assert any(isinstance(e, PlanTouched) for e in events)


def test_unknown_call_id_does_not_raise():
    events = EventMapper().map({
        "type": "tool_execution_end", "toolCallId": "never-seen",
        "toolName": "write", "isError": False,
    })
    assert events == [ToolEnded("write", None, False)]


# ---------------------------------------------------------------------------
# Streaming text coalescing
# ---------------------------------------------------------------------------


def _delta(text: str) -> dict:
    return {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": text}}


def test_text_deltas_are_coalesced_not_emitted_per_token():
    clock = FakeClock()
    mapper = EventMapper(text_interval=0.5, clock=clock)
    emitted = []
    for token in ["Hello", " ", "world", "!"]:
        emitted += mapper.map(_delta(token))
    assert emitted == []  # nothing yet: inside the interval

    clock.advance(1.0)
    emitted += mapper.map(_delta(" Done"))
    assert emitted == [AssistantText("Hello world! Done")]


def test_text_end_forces_a_flush():
    clock = FakeClock()
    mapper = EventMapper(text_interval=10.0, clock=clock)
    mapper.map(_delta("partial"))
    events = mapper.map({
        "type": "message_update",
        "assistantMessageEvent": {"type": "text_end", "content": "partial"},
    })
    assert events == [AssistantText("partial")]


def test_settle_flushes_pending_text():
    clock = FakeClock()
    mapper = EventMapper(text_interval=10.0, clock=clock)
    mapper.map(_delta("trailing words"))
    events = mapper.map({"type": "agent_settled"})
    assert events == [AssistantText("trailing words"), Settled()]


def test_tool_start_flushes_pending_text_so_the_log_stays_ordered():
    clock = FakeClock()
    mapper = EventMapper(text_interval=10.0, clock=clock)
    mapper.map(_delta("about to run a command"))
    events = mapper.map({
        "type": "tool_execution_start", "toolCallId": "c1",
        "toolName": "bash", "args": {"command": "ls"},
    })
    assert events[0] == AssistantText("about to run a command")
    assert isinstance(events[1], ToolStarted)


def test_thinking_is_buffered_until_the_block_ends():
    mapper = EventMapper()
    assert mapper.map({
        "type": "message_update",
        "assistantMessageEvent": {"type": "thinking_delta", "delta": "hmm "},
    }) == []
    events = mapper.map({
        "type": "message_update",
        "assistantMessageEvent": {"type": "thinking_end"},
    })
    assert events == [Thinking("hmm")]


def test_generation_error_becomes_a_notice():
    events = EventMapper().map({
        "type": "message_update",
        "assistantMessageEvent": {"type": "error", "reason": "aborted"},
    })
    assert events == [Notice("error", "generation aborted")]


# ---------------------------------------------------------------------------
# Usage accounting
# ---------------------------------------------------------------------------


def test_message_end_yields_usage():
    events = EventMapper().map({
        "type": "message_end",
        "message": {
            "role": "assistant",
            "usage": {"input": 100, "output": 50, "cost": {"total": 0.00105}},
        },
    })
    assert events == [Usage(100, 50, 0.00105)]


def test_message_end_without_usage_is_silent():
    assert EventMapper().map({"type": "message_end", "message": {"role": "user"}}) == []


def test_turn_end_does_not_double_count_usage():
    """turn_end repeats the same message; counting it too would double tokens."""
    mapper = EventMapper()
    message = {"role": "assistant", "usage": {"input": 10, "output": 5, "cost": {"total": 0.1}}}
    assert mapper.map({"type": "message_end", "message": message}) == [Usage(10, 5, 0.1)]
    assert mapper.map({"type": "turn_end", "message": message, "toolResults": []}) == []


# ---------------------------------------------------------------------------
# Notices and synthetic events
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,kind",
    [
        ({"type": "auto_retry_start", "attempt": 2}, "retry"),
        ({"type": "auto_retry_end"}, "retry"),
        ({"type": "compaction_start"}, "compaction"),
        ({"type": "compaction_end"}, "compaction"),
        ({"type": "summarization_retry_scheduled"}, "retry"),
        ({"type": "extension_error", "error": "boom"}, "extension"),
        ({"type": EVENT_STDERR, "text": "warning"}, "stderr"),
        ({"type": EVENT_PROTOCOL_ERROR, "error": "bad JSON"}, "protocol"),
    ],
)
def test_notice_kinds(raw, kind):
    events = EventMapper().map(raw)
    assert len(events) == 1
    assert isinstance(events[0], Notice)
    assert events[0].kind == kind


def test_long_silences_are_explained():
    """Compaction and retry must be visible, or they look like a hang."""
    mapper = EventMapper()
    assert mapper.map({"type": "compaction_start"})[0].text
    assert mapper.map({"type": "auto_retry_start", "attempt": 1})[0].text


def test_dialog_answer_is_reported_prominently():
    events = EventMapper().map({
        "type": EVENT_DIALOG_ANSWERED,
        "method": "select",
        "title": "Allow dangerous command?",
        "policy": "cancel",
        "reply": {"cancelled": True},
    })
    assert events[0].kind == "dialog"
    assert "Allow dangerous command?" in events[0].text


def test_status_info_becomes_a_notice():
    events = EventMapper().map({
        "type": "extension_ui_info", "method": "setStatus", "text": "Z.ai: 3%",
    })
    assert events == [Notice("status", "setStatus: Z.ai: 3%")]


def test_empty_status_is_dropped():
    assert EventMapper().map({"type": "extension_ui_info", "method": "setStatus", "text": ""}) == []


def test_process_exit_is_mapped():
    events = EventMapper().map({
        "type": EVENT_PROCESS_EXITED, "returncode": 3, "stderr_tail": ["boom"],
    })
    assert events == [ProcessExited(3, ("boom",))]


def test_unknown_events_are_ignored():
    assert EventMapper().map({"type": "something_new_in_pi_0_99"}) == []
