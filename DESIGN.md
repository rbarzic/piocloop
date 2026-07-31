# piocloop — design

A port of [pyocloop](../pyocloop) that drives the **PI coding agent**
(`@earendil-works/pi-coding-agent`, `pi` on PATH) instead of OpenCode.

Same product: a Textual TUI that repeatedly asks a coding agent to execute the
next task from `PLAN.md`, until the agent writes `<plan-complete>`.
Different — and much simpler — transport.

---

## 1. Why the PI port is structurally more reliable

pyocloop talks to an HTTP server over a network socket plus a lossy SSE stream.
PI offers **`--mode rpc`: newline-delimited JSON over the subprocess's own
stdin/stdout**. That deletes entire classes of the bugs found in `ANALYSIS.md`:

| pyocloop failure | piocloop |
|---|---|
| SSE reconnect gap loses `session.idle` (H3) | No socket. A pipe cannot silently drop a record; if it closes, the process died and we *know*. |
| Fixed port 4096 collisions (D5) | No port at all. |
| Ambiguous / cross-session idle events (D2) | One process, one conversation; `agent_settled` is unambiguous. |
| Polling fallback against a non-existent field (H1) | `get_state` returns a real `isStreaming` flag — a fallback that actually works. |
| Undrained stdout deadlocks the server (H2) | Still a live hazard — **must be designed against explicitly** (§4.1). |

Verified live against `pi` 0.82.1 (`--mode rpc --no-session -nt`), the event
sequence for one prompt is:

```
response(prompt, success=true)
agent_start → turn_start → message_start → message_end → … → turn_end
agent_end
agent_settled          ← the correct "iteration is over" signal
```

**Use `agent_settled`, not `agent_end`.** Per the protocol docs, `agent_end` is
one low-level run and "may still be followed by retry, compaction, or queued
continuations"; `agent_settled` fires only once nothing more will happen
automatically. Treating `agent_end` as done would cut iterations short mid-retry.

---

## 2. Architecture

```
cli.py        typer entry point (piloop run / bootstrap)
tui.py        Textual app: header, activity log, state machine, loop worker
pi_client.py  spawns & owns `pi --mode rpc`; JSONL framing; request/response
              correlation; event fan-out; extension-UI auto-responder
pi_events.py  event → internal Message mapping (the _dispatch_sse equivalent)
plan_parser.py  UNCHANGED — port verbatim from pyocloop
```

`plan_parser.py` is agent-agnostic and correct; copy it as-is (plus tests, which
pyocloop lacks). `tui.py`'s widget/state-machine layer ports over largely intact.
`opencode_server.py` + `opencode_client.py` are **replaced wholesale** by
`pi_client.py`.

### Process model

One long-lived `pi --mode rpc` process for the whole run; per iteration send
`{"type":"new_session"}` to get a fresh context. This is much cheaper than
respawning (PI's startup does model-catalog and extension loading — the smoke
test showed several hundred ms and 5 extension callbacks). The supervisor may
still kill and respawn the process as the escalation step of the watchdog (§4.2),
so respawn must be supported anyway.

### Session-per-iteration rationale

Same as pyocloop: each iteration starts from a clean context so the agent
re-reads `PLAN.md` rather than trusting stale in-context state. `--session-dir`
is passed through so sessions remain inspectable/resumable after a run.

---

## 3. Command / event mapping

| Need | RPC |
|---|---|
| new iteration | `{"type":"new_session"}` |
| send loop prompt | `{"id":"…","type":"prompt","message":"…"}` |
| iteration finished | event `agent_settled` |
| cancel current work | `{"type":"abort"}` |
| liveness / stuck check | `{"type":"get_state"}` → `data.isStreaming` |
| model at startup | CLI `--model provider/id`, or `{"type":"set_model",…}` |
| token/cost display | `turn_end` / `message_end` message objects |
| activity log | `tool_execution_start`, `message_update` text deltas |
| plan-file edit detection | `tool_execution_end` where the edited path == plan file |

Note `file.edited` has no direct PI equivalent — piocloop watches
`tool_execution_end` for edit/write tool calls, and in any case already re-reads
`PLAN.md` on a 4 s timer.

### CLI surface

```
piloop run [OPTIONS]
  -m, --model TEXT        provider/id, e.g. zai/glm-5.2   (pass through to pi)
      --thinking TEXT     off|minimal|low|medium|high|xhigh|max
      --prompt PATH       loop prompt      [default: .loop-prompt.md]
      --plan PATH         plan file        [default: PLAN.md]
  -r, --run               start immediately
      --max-iterations N  hard stop        [default: 100]
      --iteration-timeout S  per-iteration deadline [default: 1800]
      --session-dir PATH  pass through to pi
      --tools / --exclude-tools TEXT  pass through
      --dialog-policy TEXT  cancel|allow|deny   [default: cancel]   (§4.3)
      --verbose, --log PATH, --debug
```

`-p/--port` is **dropped** (no server). pyocloop's `-a/--agent` has no PI
equivalent — PI has no named-agent concept; the nearest equivalents are
`--append-system-prompt` and `--skill`, so expose those instead rather than
faking `--agent`.

---

## 4. The three things that must not be repeated

Every hang in `ANALYSIS.md` traces to a wait with no deadline or a pipe with no
reader. These are the load-bearing requirements of the port.

### 4.1 Drain every pipe, always

`pi`'s stdout is the protocol, so it is read continuously by construction —
but **stderr must get its own concurrent drain task**. pyocloop's H2 deadlock
came from exactly this. Never merge stderr into the protocol stream (it would
corrupt JSONL framing); read it separately into the log ring buffer.

**Framing rule from the spec:** split on `\n` only, stripping an optional
trailing `\r`. Do *not* use a generic line reader — Python's `for line in
stream` splits only on `\n` for text streams and is fine, but note the docs
explicitly call out Node's `readline` as non-compliant because it also breaks on
`U+2028`/`U+2029`, which are legal inside JSON strings. Use
`asyncio.StreamReader.readuntil(b"\n")` on the **binary** stream and decode
after splitting, so multi-byte and separator characters can never desync frames.
Raise `readuntil`'s limit well above the default 64 KiB — `message_update`
events embed the full partial message and routinely exceed it.

### 4.2 Every wait has a deadline, and a defined escalation

Replace pyocloop's unbounded `while not self._idle_event.is_set()` with a
three-stage watchdog:

1. **Soft poll** — every 30 s of silence, send `get_state`. If
   `isStreaming` is `false` and no `agent_settled` arrived, the settle signal was
   missed: treat as settled. *(This is the fallback pyocloop intended; unlike
   `session.idle`, `isStreaming` genuinely exists.)*
2. **Iteration deadline** — at `--iteration-timeout` (default 30 min), send
   `abort`, log it, and wait a short grace period for settle.
3. **Process wedge** — if abort doesn't settle within ~30 s, or the pipe hits
   EOF, or `get_state` stops responding: kill the process group, respawn, and
   resume the loop at the next iteration. Count consecutive respawns; stop after
   3 with a clear error.

Combined with `--max-iterations`, **no code path can wait forever**.

### 4.3 Answer extension UI dialogs — or the loop will hang

This is a genuine new hazard and the single most important protocol detail for
an *unattended* harness. Extensions can call `ctx.ui.select()` / `confirm()` /
`input()` / `editor()`, which emit `extension_ui_request` on stdout and
**block the agent until the client sends a matching `extension_ui_response` on
stdin**. Only dialogs carrying a `timeout` field auto-resolve; the rest wait
indefinitely. A TUI harness that ignores them reproduces pyocloop's hang with a
new cause.

The smoke test on this machine already emitted five `extension_ui_request`s per
prompt (`setStatus` from the `openrouter`, `zai-usage` and `mcp` extensions).
Those are fire-and-forget — but the same channel carries blocking dialogs, and
this user has six extension packages installed.

**Requirement:** `pi_client.py` MUST implement the responder:

- fire-and-forget (`setStatus`, `notify`, `setWidget`, `setTitle`,
  `set_editor_text`) → render into the header/activity log, send nothing;
- blocking (`select`, `confirm`, `input`, `editor`) → reply immediately per
  `--dialog-policy`, default `cancel`
  (`{"type":"extension_ui_response","id":…,"cancelled":true}`), and log it
  prominently so a silently-declined permission prompt is visible.

Status text arrives with ANSI SGR escapes embedded (observed:
`[38;2;128;128;128mZ.ai:…`), so strip or translate them before writing to
the Textual log.

### 4.4 Progress stalls are a state, not a hang

Carried over from D4: track `PlanProgress.completed` across iterations. If it
does not increase for N consecutive iterations (default 3), enter a distinct
`STALLED` state, pause the loop, and say so. This is the difference between "the
model is looping on a task it can't finish" and "the harness is broken" — a
distinction pyocloop cannot make.

### 4.5 Recoverable errors must actually recover

Fix D1/D3 structurally: `_worker_loop` never `return`s on error. It catches,
increments an error streak, applies backoff, and continues; only
`--max-iterations`, a completed plan, an error streak of 3, or an explicit stop
ends the worker. `Retry` re-arms a *live* worker rather than pretending to
restart a dead one.

---

## 5. Deliberate non-goals for v0.1

- No PI *SDK* embedding (`AgentSession`) — that is Node-only; the subprocess RPC
  boundary is the whole point of a Python harness.
- No multi-agent / parallel task execution.
- No resume-mid-plan-from-session — `PLAN.md` is already the durable state.

---

## 6. Risks

| Risk | Mitigation |
|---|---|
| Blocking extension dialog stalls unattended runs | §4.3 auto-responder, default `cancel` |
| PI RPC protocol churn (0.82.x, pre-1.0) | Isolate all protocol knowledge in `pi_client.py`; pin a tested `pi` version range and assert it at startup |
| `message_update` flood (one event per token) overwhelms the TUI | Coalesce text deltas; render at most ~10 Hz; never log deltas individually |
| Local/small models never tick checkboxes | §4.4 stall detection makes it visible instead of silent |
| Project-trust prompts skipped in non-interactive mode change tool availability | Document `--approve`/`-a`; surface the effective trust state at startup |
