"""Tests for PiClient — Phase 2 of PLAN.md.

Every test here maps to a hazard in DESIGN.md §4. They run against tests/fake_pi.py,
so no network, no API key and no LLM are involved.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from piocloop.pi_client import (
    EVENT_DIALOG_ANSWERED,
    EVENT_PROCESS_EXITED,
    EVENT_PROTOCOL_ERROR,
    EVENT_STDERR,
    PiClient,
    PiExited,
    PiTimeout,
    build_argv,
    strip_ansi,
)

FAKE_PI = str(Path(__file__).parent / "fake_pi.py")


def fake_argv(scenario: str) -> list[str]:
    return [sys.executable, FAKE_PI, scenario]


async def collect_until(
    client: PiClient,
    event_type: str,
    *,
    timeout: float = 10.0,
) -> list[dict]:
    """Drain events until `event_type` arrives. Fails the test on timeout."""
    seen: list[dict] = []

    async def _pump() -> None:
        while True:
            event = await client.events.get()
            seen.append(event)
            if event.get("type") == event_type:
                return

    try:
        await asyncio.wait_for(_pump(), timeout=timeout)
    except asyncio.TimeoutError:
        pytest.fail(
            f"never saw {event_type!r} within {timeout}s; saw: "
            f"{[e.get('type') for e in seen]}"
        )
    return seen


def types_of(events: list[dict]) -> list[str]:
    return [e.get("type") for e in events]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_build_argv_minimal():
    assert build_argv() == ["pi", "--mode", "rpc"]


def test_build_argv_full():
    argv = build_argv(
        model="zai/glm-5.2",
        thinking="medium",
        session_dir="/tmp/s",
        no_session=True,
        exclude_tools="bash",
        append_system_prompt=["be terse"],
        skills=["/tmp/skill"],
        approve=True,
    )
    assert argv[:3] == ["pi", "--mode", "rpc"]
    assert "--model" in argv and "zai/glm-5.2" in argv
    assert "--thinking" in argv and "medium" in argv
    assert "--no-session" in argv
    assert "--exclude-tools" in argv and "bash" in argv
    assert "--append-system-prompt" in argv and "be terse" in argv
    assert "--skill" in argv and "/tmp/skill" in argv
    assert "--approve" in argv


def test_build_argv_no_approve():
    assert "--no-approve" in build_argv(approve=False)


def test_strip_ansi():
    assert strip_ansi("\x1b[38;2;128;128;128mZ.ai:\x1b[39m 3%") == "Z.ai: 3%"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_prompt_reaches_agent_settled():
    async with PiClient(fake_argv("normal")) as client:
        await client.prompt("hello")
        events = await collect_until(client, "agent_settled")
        assert "agent_start" in types_of(events)
        assert types_of(events)[-1] == "agent_settled"


async def test_request_correlation_returns_data():
    async with PiClient(fake_argv("normal")) as client:
        state = await client.get_state()
        assert state["isStreaming"] is False
        assert state["sessionId"] == "fake"


async def test_new_session_and_abort():
    async with PiClient(fake_argv("normal")) as client:
        assert await client.new_session() == {"cancelled": False}
        await client.abort()
        await collect_until(client, "agent_settled")


async def test_concurrent_requests_are_correlated():
    """Interleaved commands must not cross-resolve each other's futures."""
    async with PiClient(fake_argv("normal")) as client:
        results = await asyncio.gather(
            client.get_state(), client.get_state(), client.get_state()
        )
        assert all(r["sessionId"] == "fake" for r in results)
        assert client._pending == {}


# ---------------------------------------------------------------------------
# Framing (DESIGN §4.1)
# ---------------------------------------------------------------------------


async def test_frames_split_across_writes_are_reassembled():
    async with PiClient(fake_argv("split")) as client:
        await client.prompt("hi")
        events = await collect_until(client, "agent_settled")
        assert "agent_start" in types_of(events)
        assert "turn_start" in types_of(events)


async def test_unicode_separators_do_not_split_frames():
    """U+2028/U+2029 are legal inside JSON strings; only LF delimits records."""
    async with PiClient(fake_argv("u2028")) as client:
        await client.prompt("hi")
        events = await collect_until(client, "agent_settled")
        assert EVENT_PROTOCOL_ERROR not in types_of(events)
        msg = next(e for e in events if e.get("type") == "message_end")
        assert msg["message"]["text"] == "a\u2028b\u2029c"


async def test_crlf_is_tolerated():
    async with PiClient(fake_argv("crlf")) as client:
        await client.prompt("hi")
        await collect_until(client, "agent_settled")


async def test_malformed_frame_does_not_kill_the_stream():
    async with PiClient(fake_argv("badjson")) as client:
        await client.prompt("hi")
        events = await collect_until(client, "agent_settled")
        assert EVENT_PROTOCOL_ERROR in types_of(events)  # reported...
        assert "agent_settled" in types_of(events)       # ...but recovered


# ---------------------------------------------------------------------------
# Extension UI (DESIGN §4.3)
# ---------------------------------------------------------------------------


async def test_blocking_dialog_is_answered_so_the_agent_can_settle():
    """The regression test for the new hang vector.

    fake_pi emits a `select` with no timeout and will not settle until it gets
    an extension_ui_response. A client that ignores dialogs hangs here.
    """
    async with PiClient(fake_argv("dialog"), dialog_policy="cancel") as client:
        await client.prompt("do something risky")
        events = await collect_until(client, "agent_settled")
        answered = next(e for e in events if e.get("type") == EVENT_DIALOG_ANSWERED)
        assert answered["method"] == "select"
        assert answered["reply"]["cancelled"] is True
        assert answered["reply"]["id"] == "dlg-1"
        resolved = next(e for e in events if e.get("type") == "dialog_resolved")
        assert resolved["reply"]["type"] == "extension_ui_response"


async def test_dialog_policy_allow_selects_first_option():
    async with PiClient(fake_argv("dialog"), dialog_policy="allow") as client:
        await client.prompt("go")
        events = await collect_until(client, "agent_settled")
        answered = next(e for e in events if e.get("type") == EVENT_DIALOG_ANSWERED)
        assert answered["reply"]["value"] == "Allow"


async def test_fire_and_forget_status_is_surfaced_but_not_answered():
    """Answering setStatus would be a protocol violation; fake_pi reports it."""
    async with PiClient(fake_argv("status")) as client:
        await client.prompt("hi")
        events = await collect_until(client, "agent_settled")
        assert "unexpected_response_received" not in types_of(events)
        info = next(e for e in events if e.get("type") == "extension_ui_info")
        assert info["method"] == "setStatus"
        assert info["text"] == "Z.ai: 3%"  # ANSI stripped


# ---------------------------------------------------------------------------
# Pipe drainage (DESIGN §4.1 / pyocloop bug H2)
# ---------------------------------------------------------------------------


async def test_stderr_flood_does_not_deadlock():
    """>512 KiB on stderr. Undrained, this blocks the child forever."""
    async with PiClient(fake_argv("stderr_flood")) as client:
        await client.prompt("hi")
        events = await collect_until(client, "agent_settled", timeout=30.0)
        assert EVENT_STDERR in types_of(events)
        assert len(client.stderr_tail) > 0


# ---------------------------------------------------------------------------
# Failure modes (DESIGN §4.2)
# ---------------------------------------------------------------------------


async def test_process_exit_is_reported_not_hung():
    client = PiClient(fake_argv("eof"))
    await client.start()
    try:
        await client.prompt("hi")
        events = await collect_until(client, EVENT_PROCESS_EXITED)
        exit_event = events[-1]
        assert exit_event["returncode"] == 3
        assert client.exited.is_set()
    finally:
        await client.close()


async def test_request_after_exit_raises():
    client = PiClient(fake_argv("eof"))
    await client.start()
    try:
        await client.prompt("hi")
        await collect_until(client, EVENT_PROCESS_EXITED)
        with pytest.raises(PiExited):
            await client.get_state(timeout=5.0)
    finally:
        await client.close()


async def test_unanswered_command_times_out():
    """No wait is unbounded: a deaf agent raises rather than hanging."""
    async with PiClient(fake_argv("deaf")) as client:
        with pytest.raises(PiTimeout):
            await client.prompt("hi", timeout=1.0)
        assert client._pending == {}  # the dead future is not leaked


async def test_close_is_idempotent():
    client = PiClient(fake_argv("normal"))
    await client.start()
    await client.close()
    await client.close()
    assert client.exited.is_set()


async def test_missing_binary_raises_clean_error():
    client = PiClient(["/nonexistent/pi-binary", "--mode", "rpc"])
    with pytest.raises(Exception) as exc:
        await client.start()
    assert "pi-binary" in str(exc.value)
