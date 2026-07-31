"""Map raw PI RPC events onto the small vocabulary the TUI cares about.

Kept free of Textual imports so it can be unit-tested as pure data-in/data-out.
`tui.py` turns these dataclasses into Textual messages.

Wire-format facts this module relies on (verified against pi 0.82.1, not assumed):

* `tool_execution_start` carries `args`; `tool_execution_end` does **not**
  (`args` is null there), so paths must be correlated by `toolCallId`.
* Built-in tools are `read`, `bash`, `edit`, `write`, `grep`, `find`, `ls`, and
  file-taking tools use the key `path` with an absolute value.
* Token usage lives on the message in `message_end`; `turn_end` repeats the same
  message, so usage is counted from `message_end` only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .pi_client import (
    EVENT_DIALOG_ANSWERED,
    EVENT_PROCESS_EXITED,
    EVENT_PROTOCOL_ERROR,
    EVENT_STDERR,
)

# Tools whose `path` argument means "this file was modified".
WRITE_TOOLS = frozenset({"edit", "write"})


# ---------------------------------------------------------------------------
# Event vocabulary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoopEvent:
    """Base class for everything the TUI consumes."""


@dataclass(frozen=True)
class Settled(LoopEvent):
    """The iteration is fully finished.

    Emitted for `agent_settled` only — never for `agent_end`, which may be
    followed by an automatic retry, compaction, or a queued continuation.
    """


@dataclass(frozen=True)
class Started(LoopEvent):
    pass


@dataclass(frozen=True)
class ToolStarted(LoopEvent):
    tool: str
    detail: str = ""
    path: Optional[str] = None


@dataclass(frozen=True)
class ToolEnded(LoopEvent):
    tool: str
    path: Optional[str] = None
    is_error: bool = False


@dataclass(frozen=True)
class PlanTouched(LoopEvent):
    """A write/edit tool wrote to the plan file — reload it."""

    path: str


@dataclass(frozen=True)
class AssistantText(LoopEvent):
    text: str


@dataclass(frozen=True)
class Thinking(LoopEvent):
    text: str


@dataclass(frozen=True)
class Usage(LoopEvent):
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0


@dataclass(frozen=True)
class Notice(LoopEvent):
    """Something worth showing in the activity log.

    `kind` is one of: retry, compaction, status, dialog, extension, stderr,
    protocol, error.
    """

    kind: str
    text: str


@dataclass(frozen=True)
class ProcessExited(LoopEvent):
    returncode: Optional[int]
    stderr_tail: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Mapper
# ---------------------------------------------------------------------------


class EventMapper:
    """Stateful translator: raw event dict in, zero or more LoopEvents out.

    State is needed for two things: correlating tool paths across
    start/end, and coalescing streaming text deltas so a token-per-event stream
    cannot flood the TUI.
    """

    def __init__(
        self,
        plan_file: Optional[Path] = None,
        *,
        text_interval: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._plan_file = plan_file.resolve() if plan_file else None
        self._text_interval = text_interval
        self._clock = clock
        self._tool_paths: dict[str, tuple[str, Optional[str]]] = {}
        self._text_buf: list[str] = []
        self._think_buf: list[str] = []
        # Seed from the clock, not 0.0: otherwise the very first delta is
        # always "overdue" and gets emitted on its own, defeating coalescing.
        self._last_text_flush = clock()

    # -- helpers -------------------------------------------------------

    def _is_plan(self, path: Optional[str]) -> bool:
        if not path or self._plan_file is None:
            return False
        try:
            return Path(path).resolve() == self._plan_file
        except OSError:
            return False

    def _flush_text(self, force: bool = False) -> list[LoopEvent]:
        if not self._text_buf:
            return []
        now = self._clock()
        if not force and (now - self._last_text_flush) < self._text_interval:
            return []
        text = "".join(self._text_buf).strip()
        self._text_buf.clear()
        self._last_text_flush = now
        return [AssistantText(text)] if text else []

    def _flush_thinking(self) -> list[LoopEvent]:
        if not self._think_buf:
            return []
        text = "".join(self._think_buf).strip()
        self._think_buf.clear()
        return [Thinking(text)] if text else []

    # -- main entry point ----------------------------------------------

    def map(self, raw: dict) -> list[LoopEvent]:
        etype = raw.get("type", "")
        handler = getattr(self, f"_on_{etype}", None)
        if handler is not None:
            return handler(raw)
        return self._on_other(etype, raw)

    # -- lifecycle ------------------------------------------------------

    def _on_agent_start(self, raw: dict) -> list[LoopEvent]:
        return [Started()]

    def _on_agent_settled(self, raw: dict) -> list[LoopEvent]:
        # Flush anything buffered so the log is complete before the iteration ends.
        return [*self._flush_text(force=True), Settled()]

    def _on_agent_end(self, raw: dict) -> list[LoopEvent]:
        if raw.get("willRetry"):
            return [Notice("retry", "agent run failed — retrying automatically")]
        return []

    # -- tools ----------------------------------------------------------

    def _on_tool_execution_start(self, raw: dict) -> list[LoopEvent]:
        tool = raw.get("toolName", "?")
        args = raw.get("args") or {}
        path = args.get("path") if isinstance(args, dict) else None
        call_id = raw.get("toolCallId")
        if call_id:
            self._tool_paths[call_id] = (tool, path)
        return [*self._flush_text(force=True), ToolStarted(tool, _tool_detail(tool, args), path)]

    def _on_tool_execution_end(self, raw: dict) -> list[LoopEvent]:
        call_id = raw.get("toolCallId")
        tool = raw.get("toolName") or "?"
        path = None
        if call_id and call_id in self._tool_paths:
            remembered_tool, path = self._tool_paths.pop(call_id)
            tool = raw.get("toolName") or remembered_tool

        is_error = bool(raw.get("isError"))
        events: list[LoopEvent] = [ToolEnded(tool, path, is_error)]
        # Only a successful write/edit means the plan actually changed.
        if not is_error and tool in WRITE_TOOLS and self._is_plan(path):
            events.append(PlanTouched(str(path)))
        return events

    # -- streaming ------------------------------------------------------

    def _on_message_update(self, raw: dict) -> list[LoopEvent]:
        delta = raw.get("assistantMessageEvent") or {}
        dtype = delta.get("type")

        if dtype == "text_delta":
            self._text_buf.append(delta.get("delta", ""))
            return self._flush_text()
        if dtype == "text_end":
            return self._flush_text(force=True)
        if dtype == "thinking_delta":
            self._think_buf.append(delta.get("delta", ""))
            return []
        if dtype == "thinking_end":
            return self._flush_thinking()
        if dtype == "error":
            reason = delta.get("reason", "error")
            if reason == "aborted":
                return [Notice("error", "generation aborted")]
            return [Notice("error", f"generation error: {reason}")]
        return []

    def _on_message_end(self, raw: dict) -> list[LoopEvent]:
        message = raw.get("message") or {}
        usage = message.get("usage") or {}
        if not usage:
            return []
        cost = usage.get("cost") or {}
        return [
            Usage(
                input_tokens=int(usage.get("input") or 0),
                output_tokens=int(usage.get("output") or 0),
                cost=float(cost.get("total") or 0.0),
            )
        ]

    # -- notices --------------------------------------------------------

    def _on_auto_retry_start(self, raw: dict) -> list[LoopEvent]:
        attempt = raw.get("attempt")
        suffix = f" (attempt {attempt})" if attempt is not None else ""
        return [Notice("retry", f"auto-retry after transient error{suffix}")]

    def _on_auto_retry_end(self, raw: dict) -> list[LoopEvent]:
        return [Notice("retry", "auto-retry finished")]

    def _on_compaction_start(self, raw: dict) -> list[LoopEvent]:
        return [Notice("compaction", "compacting context — this can take a while")]

    def _on_compaction_end(self, raw: dict) -> list[LoopEvent]:
        return [Notice("compaction", "compaction finished")]

    def _on_extension_error(self, raw: dict) -> list[LoopEvent]:
        return [Notice("extension", str(raw.get("error") or "extension error"))]

    def _on_extension_ui_info(self, raw: dict) -> list[LoopEvent]:
        text = raw.get("text") or ""
        if not text:
            return []
        return [Notice("status", f"{raw.get('method', 'ui')}: {text}")]

    def _on_queue_update(self, raw: dict) -> list[LoopEvent]:
        pending = len(raw.get("steering") or []) + len(raw.get("followUp") or [])
        if not pending:
            return []
        return [Notice("status", f"{pending} queued message(s)")]

    # -- synthetic (from PiClient) --------------------------------------

    def _on_other(self, etype: str, raw: dict) -> list[LoopEvent]:
        if etype == EVENT_STDERR:
            return [Notice("stderr", raw.get("text", ""))]
        if etype == EVENT_PROTOCOL_ERROR:
            return [Notice("protocol", raw.get("error", "protocol error"))]
        if etype == EVENT_DIALOG_ANSWERED:
            title = raw.get("title") or raw.get("method")
            return [
                Notice(
                    "dialog",
                    f"auto-answered {raw.get('method')} dialog "
                    f"({raw.get('policy')}): {title}",
                )
            ]
        if etype == EVENT_PROCESS_EXITED:
            return [
                ProcessExited(
                    raw.get("returncode"),
                    tuple(raw.get("stderr_tail") or ()),
                )
            ]
        if etype.startswith("summarization_retry"):
            return [Notice("retry", etype.replace("_", " "))]
        return []


def _tool_detail(tool: str, args: Any) -> str:
    """A short one-line description of a tool call for the activity log."""
    if not isinstance(args, dict):
        return ""
    if tool == "bash":
        return str(args.get("command", ""))[:120]
    for key in ("path", "pattern", "query"):
        if key in args:
            return str(args[key])[:120]
    return ""
