"""End-to-end tests for LoopEngine — Phase 6 of PLAN.md.

Everything runs against tests/fake_pi.py: no network, no API key, no LLM.

The headline guarantee under test is that the loop always terminates. Each
pathological agent below hung pyocloop; here every one must reach a definite,
resumable state within a couple of seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from pathlib import Path

import pytest

from piocloop.loop import (
    STATE_COMPLETE,
    STATE_ERROR,
    STATE_STALLED,
    LoopConfig,
    LoopEngine,
)

FAKE_PI = str(Path(__file__).parent / "fake_pi.py")


class RecordingUI:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []
        self.states: list[str] = []
        self.summary: str | None = None

    def log_line(self, kind: str, text: str, detail: str = "") -> None:
        self.lines.append((kind, text))

    def set_state(self, state: str) -> None:
        self.states.append(state)

    def on_progress(self) -> None:
        pass

    def on_complete(self, summary: str) -> None:
        self.summary = summary

    def has(self, needle: str) -> bool:
        return any(needle.lower() in text.lower() for _, text in self.lines)


@pytest.fixture
def workspace(tmp_path: Path):
    """A plan with three pending tasks plus the loop prompt."""
    plan = tmp_path / "PLAN.md"
    plan.write_text(
        "# Plan\n\n## Backlog\n\n- [ ] task one\n- [ ] task two\n- [ ] task three\n",
        encoding="utf-8",
    )
    prompt = tmp_path / ".loop-prompt.md"
    prompt.write_text("Execute the next task from {{PLAN_FILE}}.\n", encoding="utf-8")
    return plan, prompt


def make_config(workspace, scenario: str, **overrides) -> LoopConfig:
    plan, prompt = workspace
    defaults = dict(
        prompt_file=prompt,
        plan_file=plan,
        argv=[sys.executable, FAKE_PI, scenario],
        # Fast watchdog so tests exercise real code paths in milliseconds.
        soft_poll_interval=0.2,
        idle_polls_to_settle=2,
        iteration_timeout=30.0,
        abort_grace=0.3,
        command_timeout=0.3,
        error_backoff_base=1.0,
        max_iterations=20,
        max_stalls=3,
    )
    defaults.update(overrides)
    return LoopConfig(**defaults)


async def wait_for_state(engine: LoopEngine, states: set[str], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if engine.state in states:
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"engine stuck in {engine.state!r}, never reached {states}")


class EngineHarness:
    """Boot an engine, run it in the background, always clean it up."""

    def __init__(self, config: LoopConfig) -> None:
        self.ui = RecordingUI()
        self.engine = LoopEngine(config, ui=self.ui)
        self.task: asyncio.Task | None = None

    async def __aenter__(self) -> "EngineHarness":
        assert await self.engine.boot(), "fake pi failed to boot"
        self.task = asyncio.create_task(self.engine.run(auto_start=True))
        return self

    async def __aexit__(self, *_: object) -> None:
        self.engine.request_stop()
        if self.task is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(self.task, timeout=10.0)
            self.task.cancel()
        await self.engine.shutdown()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_plan_runs_to_completion(workspace):
    plan, _ = workspace
    async with EngineHarness(make_config(workspace, "planworker")) as h:
        await asyncio.wait_for(h.task, timeout=30.0)

        assert h.engine.state == STATE_COMPLETE
        assert "<plan-complete>" in plan.read_text(encoding="utf-8")
        assert h.ui.summary == "fake agent finished the plan"
        # three tasks ticked, plus one iteration that appended the marker
        assert h.engine.iteration == 4
        assert h.engine.progress is not None
        assert h.engine.progress.completed == 3
        assert h.engine.progress.pending == 0


async def test_plan_edits_are_detected(workspace):
    async with EngineHarness(make_config(workspace, "planworker")) as h:
        await asyncio.wait_for(h.task, timeout=30.0)
        assert h.ui.has("plan file updated")


# ---------------------------------------------------------------------------
# Termination guarantees — each of these hung pyocloop
# ---------------------------------------------------------------------------


async def test_silent_agent_recovers_via_soft_poll(workspace):
    """Prompt accepted, then permanent silence — pyocloop's H1/H3 hang.

    The agent honestly reports it is not streaming, so the soft poll must
    conclude the settle event was missed and move on.
    """
    async with EngineHarness(make_config(workspace, "silent", max_stalls=1)) as h:
        await wait_for_state(h.engine, {STATE_STALLED}, timeout=20.0)
        assert h.ui.has("idle but no settle event")
        assert h.engine.iteration >= 1


async def test_busy_agent_is_aborted_at_the_deadline(workspace):
    """Agent claims to be streaming forever: only the deadline can stop it."""
    config = make_config(workspace, "busy", iteration_timeout=1.0, max_stalls=1)
    async with EngineHarness(config) as h:
        await wait_for_state(h.engine, {STATE_STALLED}, timeout=25.0)
        assert h.ui.has("exceeded")
        assert h.ui.has("aborting")


async def test_wedged_agent_is_respawned_then_gives_up(workspace):
    """Agent ignores abort too: escalate to a process restart, then stop."""
    config = make_config(
        workspace, "wedged",
        iteration_timeout=1.0,
        max_respawns=1,
        max_error_streak=2,
        max_stalls=0,
    )
    async with EngineHarness(config) as h:
        await wait_for_state(h.engine, {STATE_ERROR}, timeout=30.0)
        assert h.engine.respawns >= 1
        assert h.ui.has("restarting pi")
        assert h.ui.has("consecutive failures")


async def test_blocking_dialog_does_not_stall_the_loop(workspace):
    """A dialog with no timeout mid-iteration must not wedge the run."""
    plan, _ = workspace
    async with EngineHarness(make_config(workspace, "dialogworker")) as h:
        await asyncio.wait_for(h.task, timeout=30.0)
        assert h.engine.state == STATE_COMPLETE
        assert "<plan-complete>" in plan.read_text(encoding="utf-8")
        assert h.ui.has("auto-answered confirm dialog")


async def test_agent_exit_is_survivable(workspace):
    """`pi` dying mid-run becomes a reported error, not a hang."""
    config = make_config(workspace, "eof", max_error_streak=2, max_stalls=0)
    async with EngineHarness(config) as h:
        await wait_for_state(h.engine, {STATE_ERROR}, timeout=25.0)
        assert h.ui.has("pi exited")


# ---------------------------------------------------------------------------
# Stall detection (DESIGN §4.4)
# ---------------------------------------------------------------------------


async def test_stall_detection_stops_a_looping_agent(workspace):
    """The agent works but never ticks a box — pyocloop bug D4."""
    async with EngineHarness(make_config(workspace, "stubborn", max_stalls=2)) as h:
        await wait_for_state(h.engine, {STATE_STALLED}, timeout=25.0)
        assert h.engine.stall_count == 2
        assert h.ui.has("no progress for 2 iterations")


async def test_stall_can_be_overridden_with_retry(workspace):
    async with EngineHarness(make_config(workspace, "stubborn", max_stalls=1)) as h:
        await wait_for_state(h.engine, {STATE_STALLED}, timeout=25.0)
        iterations_at_stall = h.engine.iteration

        h.engine.request_retry()
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and h.engine.iteration <= iterations_at_stall:
            await asyncio.sleep(0.05)
        assert h.engine.iteration > iterations_at_stall, "retry did not resume the loop"


async def test_stall_detection_can_be_disabled(workspace):
    config = make_config(workspace, "stubborn", max_stalls=0, max_iterations=3)
    async with EngineHarness(config) as h:
        await wait_for_state(h.engine, {STATE_ERROR}, timeout=25.0)
        assert h.engine.iteration == 3  # stopped by max-iterations, not by stall


# ---------------------------------------------------------------------------
# Bounds and controls
# ---------------------------------------------------------------------------


async def test_max_iterations_is_enforced(workspace):
    config = make_config(workspace, "stubborn", max_iterations=2, max_stalls=0)
    async with EngineHarness(config) as h:
        await wait_for_state(h.engine, {STATE_ERROR}, timeout=25.0)
        assert h.engine.iteration == 2
        assert h.ui.has("reached max-iterations")


async def test_retry_after_max_iterations_extends_the_budget(workspace):
    config = make_config(workspace, "planworker", max_iterations=1, max_stalls=0)
    async with EngineHarness(config) as h:
        await wait_for_state(h.engine, {STATE_ERROR}, timeout=25.0)
        assert h.engine.config.max_iterations == 1
        h.engine.request_retry()
        assert h.engine.config.max_iterations == 11


async def test_stop_request_ends_the_loop(workspace):
    async with EngineHarness(make_config(workspace, "stubborn", max_stalls=0)) as h:
        await asyncio.sleep(0.3)
        h.engine.request_stop()
        await asyncio.wait_for(h.task, timeout=10.0)
        assert h.engine.stopping


async def test_boot_failure_is_reported(workspace):
    config = make_config(workspace, "normal")
    config.argv = ["/nonexistent/pi-binary"]
    engine = LoopEngine(config, ui=RecordingUI())
    assert await engine.boot() is False
    assert engine.state == STATE_ERROR


async def test_pause_stops_after_the_current_iteration(workspace):
    """Pause must not interrupt work mid-iteration (pyocloop bug D6 clarified)."""
    from piocloop.loop import STATE_PAUSED

    async with EngineHarness(make_config(workspace, "stubborn", max_stalls=0)) as h:
        h.engine.request_pause()
        await wait_for_state(h.engine, {STATE_PAUSED}, timeout=20.0)
        paused_at = h.engine.iteration

        await asyncio.sleep(0.5)
        assert h.engine.iteration == paused_at, "loop kept running while paused"

        h.engine.request_resume()
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and h.engine.iteration == paused_at:
            await asyncio.sleep(0.05)
        assert h.engine.iteration > paused_at
