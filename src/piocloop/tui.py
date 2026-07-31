"""Textual TUI for piocloop — a thin view over LoopEngine.

All orchestration lives in `loop.py` so it can be tested without a terminal;
this module only renders state and translates key presses into engine calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime
from pathlib import Path
from typing import IO, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, RichLog, Static

from .loop import (
    STATE_COMPLETE,
    STATE_ERROR,
    STATE_PAUSED,
    STATE_PAUSING,
    STATE_READY,
    STATE_RUNNING,
    STATE_STALLED,
    STATE_STARTING,
    LoopConfig,
    LoopEngine,
)

_STATE_ICONS = {
    STATE_STARTING: "◐",
    STATE_READY: "●",
    STATE_RUNNING: "▶",
    STATE_PAUSING: "◑",
    STATE_PAUSED: "⏸",
    STATE_STALLED: "⚠",
    STATE_COMPLETE: "✓",
    STATE_ERROR: "✗",
}

_STATE_COLORS = {
    STATE_STARTING: "yellow",
    STATE_READY: "cyan",
    STATE_RUNNING: "green",
    STATE_PAUSING: "yellow",
    STATE_PAUSED: "yellow",
    STATE_STALLED: "orange1",
    STATE_COMPLETE: "bright_green",
    STATE_ERROR: "red",
}

_LOG_COLORS = {
    "start": "cyan", "idle": "cyan", "task": "yellow", "edit": "green",
    "error": "red", "tool": "magenta", "read": "blue", "ai": "white",
    "think": "dim", "info": "dim", "complete": "bright_green",
    "warn": "orange1", "dialog": "orange1", "retry": "orange1",
    "compaction": "blue", "status": "dim", "stderr": "dim",
    "extension": "orange1", "protocol": "orange1",
}


def _escape(text: str) -> str:
    """Agent output must never be interpreted as Rich markup."""
    return text.replace("[", r"\[")


class _Header(Static):
    DEFAULT_CSS = """
    _Header {
        height: auto;
        background: $panel;
        border-bottom: solid $primary;
        padding: 0 1;
    }
    """


class PiloopApp(App):
    CSS = """
    Screen { layout: vertical; }
    _Header { height: auto; min-height: 3; }
    RichLog { height: 1fr; border: none; scrollbar-gutter: stable; }
    Footer  { height: 1; }
    """

    BINDINGS = [
        Binding("s", "start_loop", "Start", show=True),
        Binding("space", "toggle_pause", "Pause", show=True),
        Binding("a", "abort_now", "Abort iter", show=True),
        Binding("r", "retry", "Retry", show=True),
        Binding("q", "request_quit", "Quit", show=True),
        Binding("ctrl+c", "request_quit", "Quit", show=False),
    ]

    def __init__(
        self,
        config: LoopConfig,
        *,
        model: Optional[str] = None,
        auto_run: bool = False,
        log_file: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._model = model
        self._auto_run = auto_run
        self._log_fh: Optional[IO[str]] = (
            open(log_file, "a", encoding="utf-8") if log_file else None
        )
        self.engine = LoopEngine(config, ui=self)

    # ------------------------------------------------------------------
    # Compose / lifecycle
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield _Header()
        yield RichLog(id="log", auto_scroll=True, highlight=False, markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_header()
        self.run_worker(self._worker_boot(), name="boot")
        self.set_interval(1.0, self._refresh_header)
        self.set_interval(4.0, self.engine.reload_plan)

    async def _worker_boot(self) -> None:
        try:
            if not await self.engine.boot():
                return
            await self.engine.run(auto_start=self._auto_run)
        finally:
            await self.engine.shutdown()

    async def on_unmount(self) -> None:
        await self.engine.shutdown()
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None

    # ------------------------------------------------------------------
    # LoopUI implementation
    # ------------------------------------------------------------------

    def log_line(self, kind: str, text: str, detail: str = "") -> None:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        if self._log_fh:
            suffix = f" | {detail}" if detail else ""
            self._log_fh.write(f"{ts} [{kind}] {text}{suffix}\n")
            self._log_fh.flush()
        color = _LOG_COLORS.get(kind, "white")
        detail_str = f" [dim]{_escape(detail[:100])}[/dim]" if detail else ""
        with contextlib.suppress(Exception):
            self.query_one(RichLog).write(
                f"[dim]{ts}[/dim] [{color}][{kind}][/{color}] {_escape(text)}{detail_str}"
            )

    def set_state(self, state: str) -> None:
        self._refresh_header()

    def on_progress(self) -> None:
        self._refresh_header()

    def on_complete(self, summary: str) -> None:
        with contextlib.suppress(Exception):
            log = self.query_one(RichLog)
            log.write("")
            log.write("[bright_green bold]╔══════════════════════════════════════╗[/bright_green bold]")
            log.write("[bright_green bold]║           PLAN COMPLETE              ║[/bright_green bold]")
            log.write("[bright_green bold]╚══════════════════════════════════════╝[/bright_green bold]")
            log.write("")
            for line in summary.splitlines():
                log.write(f"  {_escape(line)}")
            log.write("")
            log.write("[dim]Press Q to quit.[/dim]")

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _refresh_header(self) -> None:
        with contextlib.suppress(Exception):
            self.query_one(_Header).update(self._build_header())

    def _build_header(self) -> str:
        engine = self.engine
        icon = _STATE_ICONS.get(engine.state, "?")
        color = _STATE_COLORS.get(engine.state, "white")
        state_str = f"[{color}]{icon} {engine.state.upper()}[/{color}]"

        if engine.progress:
            p = engine.progress
            automatable = p.completed + p.pending
            pct = p.percent_complete
            filled = round(20 * pct / 100)
            bar = "█" * filled + "░" * (20 - filled)
            progress_str = f"[{p.completed}/{automatable}] [{bar}] {pct}%"
            if p.blocked:
                progress_str += f" [red]{p.blocked} blocked[/red]"
        else:
            progress_str = ""

        iter_str = (
            f"iter:{engine.iteration}/{engine.config.max_iterations}"
            if engine.iteration
            else ""
        )

        elapsed_str = ""
        if engine.iter_start_time and engine.state in (STATE_RUNNING, STATE_PAUSING):
            e = int(time.monotonic() - engine.iter_start_time)
            elapsed_str = f"{e // 60}:{e % 60:02d}"
        avg_str = ""
        if engine.iter_times:
            a = int(sum(engine.iter_times) / len(engine.iter_times))
            avg_str = f"avg:{a // 60}:{a % 60:02d}"

        model_str = f"[dim]{self._model}[/dim]" if self._model else ""
        tok_str = f"[dim]tok:{engine.total_tokens:,}[/dim]" if engine.total_tokens else ""
        cost_str = f"[dim]${engine.total_cost:.4f}[/dim]" if engine.total_cost else ""

        if engine.state == STATE_COMPLETE:
            task_str = "[bright_green]All tasks complete![/bright_green]"
        elif engine.state == STATE_READY:
            task_str = "[dim]Press [bold]S[/bold] to start[/dim]"
        elif engine.state == STATE_STALLED:
            task_str = (
                f"[orange1]No progress for {engine.stall_count} iterations — "
                f"press [bold]R[/bold] to continue or [bold]Q[/bold] to quit[/orange1]"
            )
        elif engine.state == STATE_ERROR:
            task_str = "[dim]Press [bold]R[/bold] to retry or [bold]Q[/bold] to quit[/dim]"
        elif engine.state == STATE_PAUSING:
            task_str = (
                "[yellow]Finishing current iteration — "
                "press [bold]A[/bold] to abort it now[/yellow]"
            )
        elif engine.current_task:
            task_str = f"[yellow]{_escape(engine.current_task[:100])}[/yellow]"
        else:
            task_str = ""

        row1 = "  ".join(x for x in [state_str, progress_str, iter_str, elapsed_str, avg_str] if x)
        row2 = "  ".join(x for x in [model_str, tok_str, cost_str] if x)
        lines = [row1]
        if row2:
            lines.append(row2)
        if task_str:
            lines.append(task_str)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_start_loop(self) -> None:
        if self.engine.state == STATE_READY:
            self.engine.request_start()

    def action_toggle_pause(self) -> None:
        if self.engine.state == STATE_RUNNING:
            self.engine.request_pause()
            self.log_line("info", "Pause requested — will stop after this iteration")
        elif self.engine.state in (STATE_PAUSED, STATE_PAUSING):
            self.engine.request_resume()
        self._refresh_header()

    def action_abort_now(self) -> None:
        if self.engine.state in (STATE_RUNNING, STATE_PAUSING):
            self.log_line("warn", "Abort requested")
            self.run_worker(self.engine.abort_current(), name="abort")

    def action_retry(self) -> None:
        if self.engine.state in (STATE_ERROR, STATE_STALLED):
            self.log_line("info", "Retrying")
            self.engine.request_retry()

    def action_request_quit(self) -> None:
        self.engine.request_stop()
        self.run_worker(self._quit_soon(), name="quit")

    async def _quit_soon(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.engine.abort_current(), timeout=5.0)
        await self.engine.shutdown()
        self.exit()
