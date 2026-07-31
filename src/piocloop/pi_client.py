"""Client for the PI coding agent's RPC mode (`pi --mode rpc`).

All knowledge of the PI wire protocol lives in this module; the rest of piocloop
sees only `PiClient` methods and dict events.

Three design rules here are load-bearing (see DESIGN.md §4). They exist because
pyocloop hung in production for want of each one:

1. Every pipe is drained continuously. An unread stderr pipe fills and blocks
   the child process — that was pyocloop's H2 deadlock.
2. Every wait has a deadline. No `await` in this module can block forever.
3. Blocking extension UI dialogs are answered automatically. `pi` stops and
   waits for a reply to `select`/`confirm`/`input`/`editor`, and only dialogs
   carrying a `timeout` field ever self-resolve, so an unattended harness that
   ignores them hangs.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import os
import re
import signal
from typing import Any, Awaitable, Callable, Optional, Sequence

# Dialog methods block the agent until we answer. Everything else on the
# extension UI channel is fire-and-forget and must NOT be answered.
_DIALOG_METHODS = frozenset({"select", "confirm", "input", "editor"})

# Guard against a pathological frame eating all memory. Real `message_update`
# events embed the full partial message and can be large, so this is generous.
_MAX_FRAME_BYTES = 64 * 1024 * 1024

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Synthetic event types, emitted by this module rather than by `pi`.
EVENT_PROCESS_EXITED = "_process_exited"
EVENT_STDERR = "_stderr"
EVENT_PROTOCOL_ERROR = "_protocol_error"
EVENT_DIALOG_ANSWERED = "_dialog_answered"


def strip_ansi(text: str) -> str:
    """Remove SGR escapes — extension status text arrives pre-coloured."""
    return _ANSI_RE.sub("", text)


class PiError(RuntimeError):
    """A command was rejected by `pi`, or the process is unusable."""


class PiTimeout(PiError):
    """A command was not answered within its deadline."""


class PiExited(PiError):
    """The `pi` process exited while a command was in flight."""


def build_argv(
    *,
    pi_bin: str = "pi",
    model: Optional[str] = None,
    thinking: Optional[str] = None,
    session_dir: Optional[str] = None,
    no_session: bool = False,
    tools: Optional[str] = None,
    exclude_tools: Optional[str] = None,
    append_system_prompt: Sequence[str] = (),
    skills: Sequence[str] = (),
    approve: Optional[bool] = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Build the `pi --mode rpc` command line."""
    argv = [pi_bin, "--mode", "rpc"]
    if model:
        argv += ["--model", model]
    if thinking:
        argv += ["--thinking", thinking]
    if session_dir:
        argv += ["--session-dir", session_dir]
    if no_session:
        argv += ["--no-session"]
    if tools:
        argv += ["--tools", tools]
    if exclude_tools:
        argv += ["--exclude-tools", exclude_tools]
    for text in append_system_prompt:
        argv += ["--append-system-prompt", text]
    for skill in skills:
        argv += ["--skill", skill]
    if approve is True:
        argv += ["--approve"]
    elif approve is False:
        argv += ["--no-approve"]
    argv += list(extra_args)
    return argv


class PiClient:
    """Owns a `pi --mode rpc` subprocess and speaks its JSONL protocol.

    Events (including synthetic ones) are pushed to `events`; the caller is
    expected to consume that queue continuously.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Optional[str] = None,
        dialog_policy: str = "cancel",
        stderr_tail: int = 200,
        default_timeout: float = 60.0,
    ) -> None:
        self._argv = list(argv)
        self._cwd = cwd
        self._dialog_policy = dialog_policy
        self._default_timeout = default_timeout

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._tasks: list[asyncio.Task] = []
        self._pending: dict[str, asyncio.Future] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._closing = False

        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.exited: asyncio.Event = asyncio.Event()
        self.returncode: Optional[int] = None
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=stderr_tail)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._proc is not None:
            raise PiError("PiClient already started")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv,
                cwd=self._cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group, so close() kills children
            )
        except FileNotFoundError as exc:
            raise PiError(f"Could not execute {self._argv[0]!r}: {exc}") from exc

        # Both pipes get a dedicated reader for the whole process lifetime.
        # Leaving either unread eventually blocks the child (pyocloop bug H2).
        self._tasks = [
            asyncio.create_task(self._read_stdout(), name="pi-stdout"),
            asyncio.create_task(self._read_stderr(), name="pi-stderr"),
        ]

    async def close(self, timeout: float = 5.0) -> None:
        """Shut the process down. Idempotent, and safe to call after a crash."""
        self._closing = True
        proc, self._proc = self._proc, None
        if proc is not None:
            with contextlib.suppress(Exception):
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except (asyncio.TimeoutError, Exception):  # noqa: B014 - best effort
                self._kill_group(proc, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=timeout)
                except (asyncio.TimeoutError, Exception):  # noqa: B014
                    self._kill_group(proc, signal.SIGKILL)
                    with contextlib.suppress(Exception):
                        await proc.wait()
            self.returncode = proc.returncode

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        self._fail_pending(PiExited("pi process closed"))
        self.exited.set()

    @staticmethod
    def _kill_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(os.getpgid(proc.pid), sig)

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------

    async def _read_stdout(self) -> None:
        """Parse strict JSONL: records are delimited by LF and nothing else.

        Deliberately not using readline()/readuntil(): we split the raw byte
        stream on b"\\n" ourselves so that neither a stream-buffer limit nor a
        Unicode line separator (U+2028/U+2029, legal inside JSON strings) can
        desynchronise framing.
        """
        assert self._proc and self._proc.stdout
        buf = bytearray()
        try:
            while True:
                chunk = await self._proc.stdout.read(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > _MAX_FRAME_BYTES:
                    raise PiError("RPC frame exceeded maximum size")
                while True:
                    nl = buf.find(b"\n")
                    if nl == -1:
                        break
                    raw = bytes(buf[:nl])
                    del buf[: nl + 1]
                    self._handle_frame(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit({"type": EVENT_PROTOCOL_ERROR, "error": str(exc)})
        finally:
            await self._on_eof()

    def _handle_frame(self, raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace").rstrip("\r")
        if not line.strip():
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            # Never fatal: a malformed frame must not take the loop down.
            self._emit({"type": EVENT_PROTOCOL_ERROR, "error": f"bad JSON: {exc}", "raw": line[:500]})
            return
        if not isinstance(msg, dict):
            self._emit({"type": EVENT_PROTOCOL_ERROR, "error": "frame was not an object", "raw": line[:500]})
            return

        mtype = msg.get("type")
        if mtype == "response":
            self._resolve(msg)
        elif mtype == "extension_ui_request":
            self._handle_ui_request(msg)
        else:
            self._emit(msg)

    async def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        try:
            while True:
                chunk = await self._proc.stderr.read(65536)
                if not chunk:
                    break
                for line in chunk.decode("utf-8", errors="replace").splitlines():
                    line = line.rstrip()
                    if line:
                        self.stderr_tail.append(line)
                        self._emit({"type": EVENT_STDERR, "text": strip_ansi(line)})
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _on_eof(self) -> None:
        """stdout closed — the process is gone or going."""
        if self.exited.is_set():
            return
        proc = self._proc
        if proc is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            self.returncode = proc.returncode
        self.exited.set()
        self._fail_pending(PiExited(f"pi exited (code {self.returncode})"))
        if not self._closing:
            self._emit({
                "type": EVENT_PROCESS_EXITED,
                "returncode": self.returncode,
                "stderr_tail": list(self.stderr_tail)[-20:],
            })

    def _emit(self, event: dict) -> None:
        self.events.put_nowait(event)

    # ------------------------------------------------------------------
    # Extension UI auto-responder (DESIGN §4.3)
    # ------------------------------------------------------------------

    def _handle_ui_request(self, msg: dict) -> None:
        method = msg.get("method", "")
        if method not in _DIALOG_METHODS:
            # Fire-and-forget (setStatus/notify/setWidget/setTitle/...). Surface
            # it, but sending a response would be a protocol violation.
            self._emit({
                "type": "extension_ui_info",
                "method": method,
                "text": strip_ansi(str(
                    msg.get("statusText") or msg.get("message") or msg.get("title") or ""
                )),
                "raw": msg,
            })
            return

        reply = self._dialog_reply(msg)
        # Answer immediately: `pi` is blocked until we do.
        asyncio.create_task(self._send_raw(reply))
        self._emit({
            "type": EVENT_DIALOG_ANSWERED,
            "method": method,
            "title": strip_ansi(str(msg.get("title") or "")),
            "policy": self._dialog_policy,
            "reply": reply,
        })

    def _dialog_reply(self, msg: dict) -> dict:
        rid = msg.get("id")
        method = msg.get("method")
        policy = self._dialog_policy

        if policy == "cancel":
            return {"type": "extension_ui_response", "id": rid, "cancelled": True}

        if method == "confirm":
            return {"type": "extension_ui_response", "id": rid, "confirmed": policy == "allow"}

        if method == "select":
            options = msg.get("options") or []
            if policy == "allow" and options:
                return {"type": "extension_ui_response", "id": rid, "value": options[0]}
            return {"type": "extension_ui_response", "id": rid, "cancelled": True}

        # input / editor have no meaningful non-interactive answer.
        return {"type": "extension_ui_response", "id": rid, "cancelled": True}

    # ------------------------------------------------------------------
    # Command plumbing
    # ------------------------------------------------------------------

    async def _send_raw(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or self.exited.is_set():
            raise PiExited("pi process is not running")
        data = (json.dumps(payload) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                proc.stdin.write(data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise PiExited(f"pi stdin closed: {exc}") from exc

    async def request(
        self,
        command: dict,
        *,
        timeout: Optional[float] = None,
    ) -> dict:
        """Send a command and await its correlated response.

        Always bounded: raises PiTimeout rather than waiting indefinitely.
        """
        self._next_id += 1
        rid = f"piloop-{self._next_id}"
        payload = {**command, "id": rid}

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[rid] = future
        try:
            await self._send_raw(payload)
            data = await asyncio.wait_for(
                future, timeout=timeout if timeout is not None else self._default_timeout
            )
        except asyncio.TimeoutError as exc:
            raise PiTimeout(f"no response to {command.get('type')!r} within deadline") from exc
        finally:
            self._pending.pop(rid, None)

        if not data.get("success", False):
            raise PiError(f"{command.get('type')} failed: {data.get('error', 'unknown error')}")
        return data.get("data") or {}

    def _resolve(self, msg: dict) -> None:
        rid = msg.get("id")
        future = self._pending.pop(rid, None) if rid else None
        if future is None:
            # Uncorrelated response (e.g. a parse error for an unparseable
            # command). Surface it rather than dropping it silently.
            self._emit({"type": "uncorrelated_response", "raw": msg})
            return
        if not future.done():
            future.set_result(msg)

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def new_session(self, timeout: float = 30.0) -> dict:
        return await self.request({"type": "new_session"}, timeout=timeout)

    async def prompt(self, message: str, *, timeout: float = 60.0) -> dict:
        """Send a prompt. The response only acknowledges acceptance.

        Completion is signalled later by the `agent_settled` event.
        """
        return await self.request({"type": "prompt", "message": message}, timeout=timeout)

    async def abort(self, timeout: float = 30.0) -> dict:
        return await self.request({"type": "abort"}, timeout=timeout)

    async def get_state(self, timeout: float = 30.0) -> dict:
        return await self.request({"type": "get_state"}, timeout=timeout)

    async def set_model(self, provider: str, model_id: str, timeout: float = 30.0) -> dict:
        return await self.request(
            {"type": "set_model", "provider": provider, "modelId": model_id},
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "PiClient":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
