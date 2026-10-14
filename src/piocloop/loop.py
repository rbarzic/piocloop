"""The orchestration loop, decoupled from Textual.

Deliberate deviation from PLAN.md Phase 4, which put this in `tui.py`: the
watchdog, stall detection and error-streak logic are the parts most likely to
harbour a hang, and they are only testable in isolation if they do not need a
terminal. `tui.py` is now a thin view over this engine.

Termination guarantees (DESIGN §4.2 / §4.5):

* `_await_settle` cannot block forever — it soft-polls, then aborts at the
  iteration deadline, then respawns a wedged process.
* `run()` never exits on a recoverable error; it backs off and continues, and
  parks in a resumable state on a streak, a stall, or max-iterations.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence

from .pi_client import PiClient, PiError, PiExited, PiTimeout
from .pi_events import (
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
from .plan_parser import (
    PlanProgress,
    is_plan_complete,
    read_current_task,
    read_plan_complete_summary,
    read_plan_progress,
)
from .schedule import Schedule, ScheduleDecision

# --- states ---------------------------------------------------------------

STATE_STARTING = "starting"
STATE_READY = "ready"
STATE_RUNNING = "running"
STATE_PAUSING = "pausing"
STATE_PAUSED = "paused"
STATE_STALLED = "stalled"
STATE_COMPLETE = "complete"
STATE_ERROR = "error"


@dataclass
class LoopConfig:
    prompt_file: Path
    plan_file: Path
    argv: Sequence[str]
    cwd: Optional[str] = None
    dialog_policy: str = "cancel"
    max_iterations: int = 100
    iteration_timeout: float = 1800.0
    max_stalls: int = 3
    verbose: bool = False
    # watchdog tuning — overridable so tests need not wait 30s
    soft_poll_interval: float = 30.0
    idle_polls_to_settle: int = 2
    abort_grace: float = 30.0
    max_respawns: int = 3
    max_error_streak: int = 3
    error_backoff_base: float = 2.0
    command_timeout: float = 15.0  # deadline for get_state / abort round-trips
    schedule: Optional[Schedule] = None
    schedule_poll_interval: float = 1.0  # bounded wall-clock/suspend reevaluation


class LoopUI(Protocol):
    """Everything the engine needs from a view."""

    def log_line(self, kind: str, text: str, detail: str = "") -> None: ...
    def set_state(self, state: str) -> None: ...
    def on_progress(self) -> None: ...
    def on_complete(self, summary: str) -> None: ...


class NullUI:
    def log_line(self, kind: str, text: str, detail: str = "") -> None:
        pass

    def set_state(self, state: str) -> None:
        pass

    def on_progress(self) -> None:
        pass

    def on_complete(self, summary: str) -> None:
        pass


class LoopEngine:
    def __init__(
        self,
        config: LoopConfig,
        ui: Optional[LoopUI] = None,
        *,
        client_factory: Optional[Callable[[], PiClient]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.config = config
        self.ui: LoopUI = ui or NullUI()
        self._client_factory = client_factory or self._default_client_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if config.schedule_poll_interval <= 0:
            raise ValueError("schedule_poll_interval must be positive")
        self.schedule_decision: Optional[ScheduleDecision] = None

        self.state = STATE_STARTING
        self.iteration = 0
        self.progress: Optional[PlanProgress] = None
        self.current_task: Optional[str] = None
        self.total_tokens = 0
        self.total_cost = 0.0
        self.error_streak = 0
        self.respawns = 0
        self.stall_count = 0
        self.iter_times: list[float] = []
        self.iter_start_time: Optional[float] = None

        self._last_completed = -1
        self._paused = False  # manual hold only; never set by the schedule
        self._recovery_held = False
        self._retry_requested = False
        self._iteration_active = False
        self._loop_started = False
        self._stop_requested = False
        self._seen_agent_start = False

        self.client: Optional[PiClient] = None
        self._mapper = EventMapper(config.plan_file)
        self._event_task: Optional[asyncio.Task] = None

        self._settled = asyncio.Event()
        self._start_event = asyncio.Event()
        self._control_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------

    def _default_client_factory(self) -> PiClient:
        return PiClient(
            self.config.argv,
            cwd=self.config.cwd,
            dialog_policy=self.config.dialog_policy,
        )

    async def boot(self) -> bool:
        """Start `pi`. Returns False (and sets STATE_ERROR) if it cannot start."""
        try:
            await self._spawn_client()
        except PiError as exc:
            self._set_state(STATE_ERROR)
            self.ui.log_line("error", f"Could not start pi: {exc}")
            diagnostics = getattr(exc, "diagnostics", {})
            for line in diagnostics.get("stderr_tail", [])[-5:]:
                self.ui.log_line("stderr", str(line))
            return False
        self.reload_plan()
        if self.config.schedule is not None:
            self.ui.log_line("info", f"Schedule timezone: {self.config.schedule.timezone.key}")
            self._refresh_schedule()
        self._set_state(STATE_READY)
        self.ui.log_line("start", "pi ready")
        return True

    async def _spawn_client(self) -> None:
        client = self._client_factory()
        await client.start()
        self.client = client
        self._event_task = asyncio.create_task(self._pump_events(client), name="pi-events")

    async def shutdown(self) -> None:
        task, self._event_task = self._event_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        client, self.client = self.client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    async def _respawn_client(self) -> None:
        self.respawns += 1
        self.ui.log_line("warn", f"Restarting pi (respawn {self.respawns}/{self.config.max_respawns})")
        await self.shutdown()
        self._mapper = EventMapper(self.config.plan_file)
        try:
            await self._spawn_client()
        except (PiError, OSError) as exc:
            # shutdown() deliberately clears self.client before starting the
            # replacement.  If readiness fails, keep that invariant explicit
            # and turn the failure into an iteration error rather than letting
            # the next retry reach _run_iteration with no client.
            self.client = None
            self.ui.log_line("error", f"Could not restart pi: {exc}")
            diagnostics = getattr(exc, "diagnostics", {})
            for line in diagnostics.get("stderr_tail", [])[-5:]:
                self.ui.log_line("stderr", str(line))
            if isinstance(exc, PiError):
                raise
            raise PiError(f"Could not restart pi: {exc}") from exc

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def _pump_events(self, client: PiClient) -> None:
        while True:
            raw = await client.events.get()
            if self.config.verbose:
                self.ui.log_line("info", f"raw {raw.get('type')}", str(raw)[:120])
            for event in self._mapper.map(raw):
                self.handle_event(event)

    def handle_event(self, event) -> None:
        if isinstance(event, Started):
            self._seen_agent_start = True
            self.ui.log_line("start", "Agent started")
        elif isinstance(event, Settled):
            self.ui.log_line("idle", "Agent settled")
            self._settled.set()
        elif isinstance(event, ToolStarted):
            self.ui.log_line("tool", event.tool, event.detail)
        elif isinstance(event, ToolEnded):
            if event.is_error:
                self.ui.log_line("warn", f"{event.tool} failed", event.path or "")
        elif isinstance(event, PlanTouched):
            self.ui.log_line("edit", "plan file updated")
            self.reload_plan()
        elif isinstance(event, AssistantText):
            self.ui.log_line("ai", event.text[:200].replace("\n", " "))
        elif isinstance(event, Thinking):
            self.ui.log_line("think", event.text[:120].replace("\n", " "))
        elif isinstance(event, Usage):
            self.total_tokens += event.input_tokens + event.output_tokens
            self.total_cost += event.cost
            self.ui.on_progress()
        elif isinstance(event, Notice):
            self.ui.log_line(event.kind, event.text)
        elif isinstance(event, ProcessExited):
            self.ui.log_line("error", f"pi exited (code {event.returncode})")
            for line in event.stderr_tail[-5:]:
                self.ui.log_line("stderr", line)
            # Unblock the loop; the watchdog decides what happens next.
            self._settled.set()

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------

    def reload_plan(self) -> None:
        try:
            self.progress = read_plan_progress(self.config.plan_file)
            self.current_task = read_current_task(self.config.plan_file)
        except OSError:
            pass
        self.ui.on_progress()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self, auto_start: bool = False) -> None:
        if auto_start:
            self._start_event.set()
        await self._start_event.wait()
        if self._stop_requested:
            return

        self._loop_started = True
        self._refresh_schedule()
        self._publish_admission_state()
        self.ui.log_line("start", "Loop started")
        monitor = (
            asyncio.create_task(self._monitor_schedule(), name="schedule-monitor")
            if self.config.schedule is not None else None
        )

        try:
            while not self._stop_requested:
                self.reload_plan()

                if is_plan_complete(self.config.plan_file):
                    self._finish_complete()
                    return

                if self.iteration >= self.config.max_iterations:
                    self.ui.log_line(
                        "warn",
                        f"Reached max-iterations ({self.config.max_iterations}) — stopping",
                    )
                    self._set_state(STATE_ERROR)
                    if not await self._wait_for_resume():
                        return
                    continue

                if not await self._wait_for_admission():
                    return
                try:
                    if not await self._run_iteration():
                        return
                except (PiError, PiExited, PiTimeout, OSError) as exc:
                    if self._stop_requested:
                        return
                    self.error_streak += 1
                    self.ui.log_line("error", f"Iteration failed: {exc}")
                    if self.error_streak >= self.config.max_error_streak:
                        self.ui.log_line(
                            "error",
                            f"{self.error_streak} consecutive failures — stopping",
                        )
                        self._set_state(STATE_ERROR)
                        if not await self._wait_for_resume():
                            return
                        continue
                    backoff = self.config.error_backoff_base ** self.error_streak
                    self.ui.log_line("info", f"Retrying in {backoff:.0f}s")
                    await asyncio.sleep(backoff)
                    continue
                else:
                    self.error_streak = 0
                    self.respawns = 0

                if self._stop_requested:
                    break

                self.reload_plan()
                if is_plan_complete(self.config.plan_file):
                    self._finish_complete()
                    return
                if self._check_stall():
                    if not await self._wait_for_resume():
                        return
                    continue

        finally:
            if monitor is not None:
                monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor
            self._loop_started = False
            self.ui.log_line("info", "Loop stopped")

    async def _start_iteration_client(self) -> None:
        """Give every iteration a clean Pi process/session boundary.

        Pi's RPC server is deliberately long-lived, but its internal session
        state and event backlog are not a safe boundary between model turns.
        In particular, sending ``new_session`` after a large settled event
        burst can leave the RPC reader alive while the command response is
        never delivered.  Closing the old process also drops its pending RPCs
        and event queue; ``start`` performs a fresh readiness handshake.
        Normal rotation is not counted as a recovery respawn.
        """
        await self.shutdown()
        self._mapper = EventMapper(self.config.plan_file)
        await self._spawn_client()

    async def _run_iteration(self) -> bool:
        try:
            await self._start_iteration_client()
        except (PiError, PiExited, OSError):
            self.client = None
            raise
        prompt_text = self.config.prompt_file.read_text(encoding="utf-8").replace(
            "{{PLAN_FILE}}", str(self.config.plan_file)
        )
        # Readiness can cross a boundary. No intervening await before dispatch
        # once the gate returns True: this is the job's admission instant.
        if not await self._wait_for_admission():
            return False
        self._settled.clear()
        self._seen_agent_start = False
        assert self.client is not None
        self._iteration_active = True
        self.iteration += 1
        self.iter_start_time = time.monotonic()
        self.ui.log_line("start", f"Iteration {self.iteration}")
        self.ui.on_progress()
        try:
            await self.client.prompt(prompt_text)
            await self._await_settle()
            self.iter_times.append(time.monotonic() - self.iter_start_time)
        finally:
            self.iter_start_time = None
            self._iteration_active = False
            self._refresh_schedule()
            self._publish_admission_state()
        return True

    async def _await_settle(self) -> None:
        """Bounded wait for `agent_settled`. Cannot hang (DESIGN §4.2)."""
        if self.client is None:
            raise PiExited("pi client is unavailable")
        cfg = self.config
        deadline = time.monotonic() + cfg.iteration_timeout
        idle_polls = 0

        while not self._stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            try:
                await asyncio.wait_for(
                    self.client.settled.wait(),
                    timeout=min(cfg.soft_poll_interval, remaining),
                )
                return
            except asyncio.TimeoutError:
                pass

            # stage 1 — soft poll
            if self.client.exited.is_set():
                raise PiExited("pi exited during iteration")
            try:
                state = await self.client.get_state(timeout=cfg.command_timeout)
            except (PiTimeout, PiExited) as exc:
                self.ui.log_line("warn", f"State poll failed: {exc}")
                idle_polls = 0
                continue

            if state.get("isStreaming"):
                idle_polls = 0
                continue

            idle_polls += 1
            # Consecutive quiet polls only: a slow provider can legitimately be
            # not-yet-streaming right after the prompt is accepted.
            if idle_polls >= cfg.idle_polls_to_settle:
                self.ui.log_line("warn", "Agent idle but no settle event — continuing")
                return

        if self._stop_requested:
            return

        # stage 2 — deadline exceeded, abort
        self.ui.log_line("warn", f"Iteration exceeded {cfg.iteration_timeout:.0f}s — aborting")
        with contextlib.suppress(PiError, PiExited, PiTimeout):
            await self.client.abort(timeout=cfg.command_timeout)
        try:
            await asyncio.wait_for(self.client.settled.wait(), timeout=cfg.abort_grace)
            return
        except asyncio.TimeoutError:
            pass

        # stage 3 — wedged, respawn
        if self.respawns >= cfg.max_respawns:
            raise PiError(f"pi wedged and respawned {self.respawns} times — giving up")
        await self._respawn_client()
        raise PiTimeout("iteration aborted; pi restarted")

    # ------------------------------------------------------------------
    # Stall / pause / completion
    # ------------------------------------------------------------------

    def _check_stall(self) -> bool:
        if not self.config.max_stalls or self.progress is None:
            return False
        completed = self.progress.completed
        if completed > self._last_completed:
            self._last_completed = completed
            self.stall_count = 0
            return False

        self.stall_count += 1
        if self.stall_count < self.config.max_stalls:
            self.ui.log_line(
                "warn",
                f"No progress this iteration ({self.stall_count}/{self.config.max_stalls})",
            )
            return False

        self._set_state(STATE_STALLED)
        self.ui.log_line(
            "warn",
            f"No progress for {self.stall_count} iterations — stopping. "
            f"The agent may be stuck on: {self.current_task or 'unknown task'}",
        )
        return True

    async def _wait_for_resume(self) -> bool:
        """Recovery hold, released only by Retry (not schedule/manual resume)."""
        self._recovery_held = True
        try:
            while True:
                self._control_event.clear()
                if self._stop_requested:
                    return False
                if is_plan_complete(self.config.plan_file):
                    self._finish_complete()
                    return False
                self._refresh_schedule()
                if self._retry_requested:
                    self._retry_requested = False
                    self.stall_count = 0
                    self.error_streak = 0
                    self.ui.log_line("info", "Retry hold released")
                    return True
                await self._wait_for_control(self.config.schedule_poll_interval)
        finally:
            self._recovery_held = False
            self._publish_admission_state()

    async def _wait_for_admission(self) -> bool:
        """Wait without resetting counters or clearing a manual/recovery hold."""
        while True:
            # Flags/clock are checked after clearing the wakeup. No await in
            # between: notifications cannot be lost at the wait boundary.
            self._control_event.clear()
            if self._stop_requested:
                return False
            if is_plan_complete(self.config.plan_file):
                self._finish_complete()
                return False
            self._refresh_schedule()
            self._publish_admission_state()
            decision = self.schedule_decision
            if (
                not self._paused
                and not self._recovery_held
                and (decision is None or decision.allowed)
            ):
                return True
            timeout = self.config.schedule_poll_interval
            if decision is not None and decision.next_change is not None:
                delta = (
                    decision.next_change.astimezone(timezone.utc)
                    - self._clock().astimezone(timezone.utc)
                ).total_seconds()
                if delta > 0:
                    timeout = min(timeout, delta)
            await self._wait_for_control(timeout)

    async def _wait_for_control(self, timeout: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._control_event.wait(), timeout=timeout)

    async def _monitor_schedule(self) -> None:
        while not self._stop_requested:
            self._refresh_schedule()
            await asyncio.sleep(self.config.schedule_poll_interval)

    def _refresh_schedule(self) -> None:
        if self.config.schedule is None:
            return
        previous = self.schedule_decision
        self.schedule_decision = self.config.schedule.evaluate(self._clock())
        decision = self.schedule_decision
        if previous is None or (previous.allowed, previous.reasons) != (
            decision.allowed, decision.reasons,
        ):
            self.ui.log_line("info", self.schedule_description)
            self._control_event.set()
        self._publish_admission_state()
        if previous != decision:
            self.ui.on_progress()

    def _publish_admission_state(self) -> None:
        if (
            not self._loop_started
            or self._recovery_held
            or self.state == STATE_COMPLETE
            or self._stop_requested
        ):
            return
        blocked = self._paused or (
            self.schedule_decision is not None and not self.schedule_decision.allowed
        )
        state = STATE_RUNNING
        if blocked:
            state = STATE_PAUSING if self._iteration_active else STATE_PAUSED
        if self.state != state:
            self._set_state(state)

    @property
    def manually_paused(self) -> bool:
        return self._paused

    @property
    def schedule_description(self) -> str:
        schedule = self.config.schedule
        decision = self.schedule_decision
        if schedule is None or decision is None:
            return ""
        zone = schedule.timezone.key
        if decision.allowed:
            return f"Schedule permits new jobs ({zone})"
        description = " + ".join(decision.reasons)
        if decision.next_allowed is not None:
            return (
                f"{description}; next permitted {decision.next_allowed:%Y-%m-%d %H:%M} "
                f"{zone} ({decision.next_allowed:%Z}, UTC{decision.next_allowed:%z})"
            )
        return f"{description}; no upcoming permitted time ({zone})"

    def _finish_complete(self) -> None:
        self._set_state(STATE_COMPLETE)
        self.current_task = None
        self.reload_plan()
        summary = read_plan_complete_summary(self.config.plan_file) or "Done."
        self.ui.log_line("complete", "Plan complete!")
        self.ui.on_complete(summary)

    def _set_state(self, state: str) -> None:
        self.state = state
        self.ui.set_state(state)

    # ------------------------------------------------------------------
    # External actions
    # ------------------------------------------------------------------

    def request_start(self) -> None:
        self._start_event.set()

    def request_pause(self) -> None:
        self._paused = True
        self._publish_admission_state()
        self._control_event.set()

    def request_resume(self) -> None:
        self._paused = False
        self._publish_admission_state()
        self._control_event.set()

    def request_retry(self) -> None:
        if self.state not in (STATE_ERROR, STATE_STALLED):
            return
        if self.state == STATE_ERROR and self.iteration >= self.config.max_iterations:
            self.config.max_iterations += 10
            self.ui.log_line("info", f"max-iterations raised to {self.config.max_iterations}")
        self._retry_requested = True
        self._control_event.set()

    def request_stop(self) -> None:
        self._stop_requested = True
        self._settled.set()
        self._control_event.set()
        self._start_event.set()

    async def abort_current(self) -> None:
        if self.client is not None:
            with contextlib.suppress(PiError, PiExited, PiTimeout):
                await self.client.abort(timeout=self.config.command_timeout)

    @property
    def stopping(self) -> bool:
        return self._stop_requested
