"""Timed admission integrated with controlled jobs and an injected wall clock."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from piocloop.loop import (
    STATE_COMPLETE,
    STATE_ERROR,
    STATE_PAUSED,
    STATE_PAUSING,
    STATE_READY,
    STATE_RUNNING,
    STATE_STALLED,
    LoopConfig,
    LoopEngine,
)
from piocloop.pi_client import PiTimeout
from piocloop.schedule import Schedule


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.002)


class RecordingUI:
    def __init__(self):
        self.states = []
        self.lines = []

    def set_state(self, state):
        self.states.append(state)

    def log_line(self, kind, text, detail=""):
        self.lines.append(text)

    def on_progress(self):
        pass

    def on_complete(self, summary):
        pass


class Client:
    def __init__(self, harness):
        self.harness = harness
        self.events = asyncio.Queue()
        self.settled = asyncio.Event()
        self.exited = asyncio.Event()
        self.aborts = 0
        self.closed = False

    async def start(self):
        if self.harness.on_start:
            await self.harness.on_start(self)

    async def close(self):
        self.closed = True

    async def prompt(self, text):
        self.harness.prompts.append((self, self.harness.now))
        if self.harness.fail_prompts:
            self.harness.fail_prompts -= 1
            raise PiTimeout("controlled prompt failure")

    async def get_state(self, **kwargs):
        return {"isStreaming": True}

    async def abort(self, **kwargs):
        self.aborts += 1
        self.settled.set()


class Harness:
    def __init__(self, tmp_path, schedule, now="2026-07-17T21:00:00", **overrides):
        plan = tmp_path / "PLAN.md"
        plan.write_text("- [ ] task\n")
        prompt = tmp_path / ".loop-prompt.md"
        prompt.write_text("Work on {{PLAN_FILE}}")
        options = dict(
            plan_file=plan,
            prompt_file=prompt,
            argv=[],
            schedule=schedule,
            schedule_poll_interval=0.01,
            max_stalls=0,
            max_iterations=10,
            soft_poll_interval=0.1,
            iteration_timeout=10,
            error_backoff_base=0.01,
        )
        options.update(overrides)
        self.now = instant(now)
        self.clients = []
        self.prompts = []
        self.on_start = None
        self.fail_prompts = 0
        self.ui = RecordingUI()
        self.engine = LoopEngine(
            LoopConfig(**options),
            ui=self.ui,
            client_factory=self.factory,
            clock=lambda: self.now,
        )
        self.task = None

    def factory(self):
        client = Client(self)
        self.clients.append(client)
        return client

    def jump(self, value):
        self.now = instant(value)
        self.engine._control_event.set()

    def complete(self):
        self.engine.config.plan_file.write_text(
            "<plan-complete>Done.</plan-complete>\n"
        )

    async def __aenter__(self):
        assert await self.engine.boot()
        self.task = asyncio.create_task(self.engine.run(auto_start=True))
        return self

    async def __aexit__(self, *_):
        self.engine.request_stop()
        try:
            await asyncio.wait_for(self.task, 2)
        finally:
            await self.engine.shutdown()


async def test_startup_blocked_no_iteration_or_rotation_and_stop_is_prompt(tmp_path):
    async with Harness(tmp_path, Schedule(pause_windows=["24/7"])) as h:
        h.engine.error_streak = 2
        h.engine.stall_count = 2
        await eventually(lambda: h.engine.state == STATE_PAUSED)
        await asyncio.sleep(0.03)
        assert len(h.clients) == 1  # readiness only; no per-job process
        assert not h.prompts
        assert h.engine.iteration == 0
        assert h.engine.iter_start_time is None and not h.engine.iter_times
        assert (h.engine.error_streak, h.engine.stall_count) == (2, 2)
        h.engine.request_stop()
        await asyncio.wait_for(h.task, 0.1)
    assert all(c.closed for c in h.clients)
    assert not any(t.get_name() == "schedule-monitor" for t in asyncio.all_tasks())


async def test_schedule_does_not_start_a_ready_loop(tmp_path):
    h = Harness(tmp_path, Schedule(run_windows=["22:00-06:00"]))
    assert await h.engine.boot()
    h.task = asyncio.create_task(h.engine.run())
    try:
        h.jump("2026-07-17T23:00:00")
        await asyncio.sleep(0.02)
        assert h.engine.state == STATE_READY and not h.prompts
        h.engine.request_start()
        await eventually(lambda: len(h.prompts) == 1)
    finally:
        await h.__aexit__()


async def test_startup_resumes_at_allowed_time_after_clock_jump(tmp_path):
    async with Harness(tmp_path, Schedule(run_windows=["22:00-06:00"])) as h:
        await eventually(lambda: h.engine.state == STATE_PAUSED)
        h.jump("2026-07-17T23:00:00")  # simulate sleep past the opening
        await eventually(lambda: len(h.prompts) == 1)
        assert h.engine.state == STATE_RUNNING
        assert h.prompts[0][1] == h.now
        h.jump("2026-07-17T21:00:00")  # backward clock adjustment
        await eventually(lambda: h.engine.state == STATE_PAUSING)
        assert h.engine.iteration == 1
        assert h.clients[-1].aborts == 0


async def test_entering_pause_finishes_job_then_blocks_next(tmp_path):
    async with Harness(tmp_path, Schedule(pause_windows=["22:00-06:00"])) as h:
        await eventually(lambda: len(h.prompts) == 1)
        active = h.clients[-1]
        h.jump("2026-07-17T22:00:00")
        await eventually(lambda: h.engine.state == STATE_PAUSING)
        assert active.aborts == 0 and not active.closed
        active.settled.set()
        await eventually(lambda: h.engine.state == STATE_PAUSED)
        await asyncio.sleep(0.03)
        assert len(h.prompts) == 1
        assert len(h.engine.iter_times) == 1 and h.engine.iter_start_time is None
        assert len(h.clients) == 2
        h.jump("2026-07-18T06:00:00")
        await eventually(lambda: len(h.prompts) == 2)


async def test_pause_ending_before_job_finishes_has_no_stale_hold(tmp_path):
    async with Harness(tmp_path, Schedule(pause_windows=["22:00-06:00"])) as h:
        await eventually(lambda: len(h.prompts) == 1)
        h.jump("2026-07-17T22:00:00")
        await eventually(lambda: h.engine.state == STATE_PAUSING)
        h.jump("2026-07-18T06:00:00")
        await eventually(lambda: h.engine.state == STATE_RUNNING)
        h.clients[-1].settled.set()
        await eventually(lambda: len(h.prompts) == 2)
        assert STATE_PAUSED not in h.ui.states


@pytest.mark.parametrize("pause", [False, True])
async def test_midnight_has_no_transient_control_or_launch(tmp_path, pause):
    windows = ["Fr 22:00-24:00", "Sa 00:00-06:00"]
    schedule = Schedule(**{("pause_windows" if pause else "run_windows"): windows})
    async with Harness(tmp_path, schedule, now="2026-07-17T23:59:59") as h:
        if pause:
            await eventually(lambda: h.engine.state == STATE_PAUSED)
        else:
            await eventually(lambda: len(h.prompts) == 1)
        states = list(h.ui.states)
        logs = list(h.ui.lines)
        h.jump("2026-07-18T00:00:00")
        await asyncio.sleep(0.04)
        assert h.ui.states == states
        assert h.ui.lines == logs
        assert len(h.prompts) == (0 if pause else 1)
        assert all(c.aborts == 0 for c in h.clients)


async def test_manual_pause_persists_when_schedule_opens(tmp_path):
    async with Harness(
        tmp_path, Schedule(pause_windows=["22:00-06:00"]), now="2026-07-17T23:00:00"
    ) as h:
        await eventually(lambda: h.engine.state == STATE_PAUSED)
        h.engine.request_pause()
        h.jump("2026-07-18T06:00:00")
        await eventually(lambda: h.engine.schedule_decision.allowed)
        assert h.engine.state == STATE_PAUSED and not h.prompts
        assert h.engine.manually_paused
        h.engine.request_resume()
        await eventually(lambda: len(h.prompts) == 1)


async def test_resume_and_retry_cannot_bypass_schedule(tmp_path):
    async with Harness(tmp_path, Schedule(pause_windows=["24/7"])) as h:
        await eventually(lambda: h.engine.state == STATE_PAUSED)
        h.engine.request_resume()
        h.engine.request_retry()
        await asyncio.sleep(0.03)
        assert h.engine.state == STATE_PAUSED and not h.prompts
        assert not h.engine._retry_requested


async def test_readiness_crossing_boundary_rechecks_before_prompt(tmp_path):
    h = Harness(tmp_path, Schedule(run_windows=["21:00-22:00"]))

    async def on_start(client):
        if len(h.clients) > 1:
            h.jump("2026-07-17T22:00:00")
            await asyncio.sleep(0)

    h.on_start = on_start
    async with h:
        await eventually(lambda: len(h.clients) == 2 and h.engine.state == STATE_PAUSED)
        assert not h.prompts and h.engine.iteration == 0
        assert h.engine.iter_start_time is None and not h.engine.iter_times
        h.jump("2026-07-18T21:00:00")
        await eventually(lambda: len(h.prompts) == 1)
        assert len(h.clients) == 2


@pytest.mark.parametrize("max_stalls", [0, 1])
async def test_completion_beats_schedule_and_stall(tmp_path, max_stalls):
    async with Harness(
        tmp_path, Schedule(pause_windows=["22:00-06:00"]), max_stalls=max_stalls
    ) as h:
        await eventually(lambda: len(h.prompts) == 1)
        h.engine._last_completed = 0
        h.jump("2026-07-17T22:00:00")
        h.complete()
        h.clients[-1].settled.set()
        await asyncio.wait_for(h.task, 0.2)
        assert h.engine.state == STATE_COMPLETE
        assert len(h.prompts) == 1


async def test_external_completion_while_waiting(tmp_path):
    async with Harness(tmp_path, Schedule(pause_windows=["24/7"])) as h:
        await eventually(lambda: h.engine.state == STATE_PAUSED)
        h.complete()
        await asyncio.wait_for(h.task, 0.2)
        assert h.engine.state == STATE_COMPLETE and not h.prompts


async def test_retry_after_error_streak_remains_schedule_gated(tmp_path):
    h = Harness(tmp_path, Schedule(pause_windows=["22:00-06:00"]), max_error_streak=1)
    h.fail_prompts = 1
    async with h:
        await eventually(lambda: h.engine.state == STATE_ERROR)
        h.jump("2026-07-17T23:00:00")
        await eventually(lambda: not h.engine.schedule_decision.allowed)
        assert h.engine.state == STATE_ERROR and h.engine.error_streak == 1
        h.engine.request_resume()  # manual resume is not Retry
        await asyncio.sleep(0.02)
        assert h.engine.state == STATE_ERROR
        h.engine.request_retry()
        await eventually(lambda: h.engine.state == STATE_PAUSED)
        assert len(h.prompts) == 1
        h.jump("2026-07-18T06:00:00")
        await eventually(lambda: len(h.prompts) == 2)


async def test_automatic_retry_is_also_gated(tmp_path, monkeypatch):
    h = Harness(tmp_path, Schedule(run_windows=["21:00-22:00"]))
    h.fail_prompts = 1
    original_prompt = Client.prompt

    async def prompt_then_close_window(client, text):
        try:
            await original_prompt(client, text)
        except PiTimeout:
            h.jump("2026-07-17T22:00:00")
            raise

    monkeypatch.setattr(Client, "prompt", prompt_then_close_window)
    async with h:
        await eventually(
            lambda: h.engine.state == STATE_PAUSED and h.engine.error_streak == 1
        )
        await asyncio.sleep(0.03)
        assert len(h.prompts) == 1 and len(h.clients) == 2
        h.jump("2026-07-18T21:00:00")
        await eventually(lambda: len(h.prompts) == 2)


@pytest.mark.parametrize("hold", ["stall", "max"])
async def test_schedule_does_not_clear_recovery_holds(tmp_path, hold):
    overrides = {"max_stalls": 1} if hold == "stall" else {"max_iterations": 1}
    async with Harness(
        tmp_path, Schedule(pause_windows=["22:00-06:00"]), **overrides
    ) as h:
        await eventually(lambda: len(h.prompts) == 1)
        h.engine._last_completed = 0
        h.clients[-1].settled.set()
        target = STATE_STALLED if hold == "stall" else STATE_ERROR
        await eventually(lambda: h.engine.state == target)
        h.jump("2026-07-17T23:00:00")
        await asyncio.sleep(0.03)
        h.jump("2026-07-18T06:00:00")
        await asyncio.sleep(0.03)
        assert h.engine.state == target and len(h.prompts) == 1
        h.jump("2026-07-18T23:00:00")
        h.engine.request_retry()
        await eventually(lambda: h.engine.state == STATE_PAUSED)
        assert len(h.prompts) == 1
        h.jump("2026-07-19T06:00:00")
        await eventually(lambda: len(h.prompts) == 2)


async def test_retry_does_not_clear_a_simultaneous_manual_pause(tmp_path):
    async with Harness(tmp_path, Schedule(), max_iterations=1) as h:
        await eventually(lambda: len(h.prompts) == 1)
        h.engine.request_pause()
        h.clients[-1].settled.set()
        await eventually(lambda: h.engine.state == STATE_ERROR)
        h.engine.request_retry()
        await eventually(lambda: h.engine.state == STATE_PAUSED)
        assert h.engine.manually_paused and len(h.prompts) == 1
        h.engine.request_resume()
        await eventually(lambda: len(h.prompts) == 2)


async def test_cancellation_cleans_up_monitor(tmp_path):
    h = Harness(tmp_path, Schedule(pause_windows=["24/7"]))
    await h.__aenter__()
    try:
        await eventually(lambda: h.engine.state == STATE_PAUSED)
        h.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await h.task
        assert not any(t.get_name() == "schedule-monitor" for t in asyncio.all_tasks())
    finally:
        h.engine.request_stop()
        await h.engine.shutdown()


async def test_watchdog_still_aborts_an_active_job_during_scheduled_pause(tmp_path):
    async with Harness(
        tmp_path, Schedule(pause_windows=["22:00-06:00"]), iteration_timeout=0.05
    ) as h:
        await eventually(lambda: len(h.prompts) == 1)
        h.jump("2026-07-17T23:00:00")
        await eventually(lambda: h.engine.state == STATE_PAUSED)
        assert h.clients[-1].aborts == 1
        assert len(h.prompts) == 1
