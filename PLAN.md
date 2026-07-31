# piocloop — implementation plan

Port of `../pyocloop` to the PI coding agent (`pi --mode rpc`).
Read `DESIGN.md` before starting; it defines the architecture and the three
reliability requirements (§4) that this plan implements.

## Overview

Build a Python 3.11+ Textual TUI (`piloop`) that repeatedly drives `pi` to
execute the next task from `PLAN.md`, stopping when the agent appends
`<plan-complete>`. Transport is newline-delimited JSON over the `pi` subprocess's
stdin/stdout.

## Backlog

### Phase 1 — Skeleton

- [x] Create `pyproject.toml`: name `piocloop`, version `0.1.0`, requires-python `>=3.11`, deps `textual>=0.80`, `typer>=0.12` (no `httpx`/`httpx-sse` — no HTTP transport), script `piloop = "piocloop.cli:app"`, hatchling wheel target `src/piocloop`
- [x] Create `src/piocloop/__init__.py` and `__main__.py`
- [x] Copy `plan_parser.py` verbatim from pyocloop — it is agent-agnostic and correct
- [x] Add `tests/test_plan_parser.py` covering completed/pending/manual/blocked parsing, `percent_complete` with blocked items, and `parse_plan_complete` picking the LAST match
- [x] Add `.gitignore` (`__pycache__`, `*.pyc`, `dist/`, `.venv/`, `.pi/`) and MIT `LICENSE`

### Phase 2 — RPC client (`pi_client.py`)

- [x] Implement `PiProcess`: spawn `pi --mode rpc` with `stdin/stdout/stderr=PIPE` and `start_new_session=True`; build argv from model/thinking/session-dir/tools options
- [x] Implement strict JSONL framing: read the **binary** stdout stream with `readuntil(b"\n")`, limit raised to >= 1 MiB, strip a trailing `\r`, then decode UTF-8 and `json.loads`; skip blank lines; log-and-continue on a malformed frame
- [x] Spawn a **separate concurrent stderr drain task** into a bounded ring buffer — never merge stderr into the protocol stream, and never leave a pipe unread (this is pyocloop bug H2)
- [x] Implement request/response correlation: monotonic `id`, `asyncio.Future` per in-flight command, resolved on the matching `{"type":"response"}`; every `await` on a future carries a timeout
- [x] Implement the event fan-out queue feeding the TUI
- [x] Implement `new_session()`, `prompt(text)`, `abort()`, `get_state()`, `set_model()`
- [x] Implement the **extension UI auto-responder** (DESIGN §4.3): fire-and-forget methods (`setStatus`, `notify`, `setWidget`, `setTitle`, `set_editor_text`) render to the log and send nothing; blocking dialogs (`select`, `confirm`, `input`, `editor`) get an immediate reply per `--dialog-policy` (default `{"cancelled": true}`) and are logged prominently
- [x] Strip ANSI SGR escape sequences from extension status text before rendering
- [x] Implement `close()`: `abort` → close stdin → wait briefly → `killpg(SIGTERM)` → `SIGKILL` escalation; must be idempotent
- [x] Add `tests/test_pi_client.py` with a **fake `pi`** script (a small Python program that emits canned JSONL) — cover: normal settle, split/partial frames, a frame containing `U+2028`, a blocking dialog, stderr flood without deadlock, and EOF mid-iteration

### Phase 3 — Event mapping (`pi_events.py`)

- [x] Map `agent_settled` → `_IterationSettled` (NOT `agent_end` — it can be followed by retry/compaction/queued continuations)
- [x] Map `tool_execution_start` → `_ToolUsed`; detect edit/write tool calls whose target resolves to the plan file and trigger a plan reload
- [x] Map `message_update` text deltas → coalesced assistant text (render at most ~10 Hz; never log per-delta)
- [x] Map thinking deltas → dim `think` log lines
- [x] Map `turn_end` / `message_end` → token + cost accounting for the header
- [x] Map `auto_retry_start` / `auto_retry_end`, `compaction_start` / `compaction_end` → visible log lines (these explain long silences and must not be mistaken for a hang)
- [x] Map `extension_error` → visible warning
- [x] Add `tests/test_pi_events.py` using recorded event fixtures

### Phase 4 — TUI and loop (`tui.py`)

- [x] Port the header widget, activity `RichLog`, state machine, and key bindings from pyocloop's `tui.py`
- [x] Add the `STALLED` state and its icon/colour alongside the existing states
- [x] Implement `_worker_loop`: reload plan → check `<plan-complete>` → `new_session` → send prompt (with `{{PLAN_FILE}}` replaced by the absolute plan path) → await settle → repeat
- [x] Implement the **three-stage watchdog** (DESIGN §4.2): 30 s soft poll via `get_state().isStreaming`; `--iteration-timeout` → `abort` + grace; then kill/respawn, aborting the run after 3 consecutive respawns
- [x] Enforce `--max-iterations` (default 100) as a hard stop
- [x] Implement **stall detection** (DESIGN §4.4): if `PlanProgress.completed` does not increase for 3 consecutive iterations, enter `STALLED`, pause, and explain in the log
- [x] Make the loop worker **error-tolerant** (DESIGN §4.5): never `return` on error — catch, increment an error streak, back off exponentially, continue; stop only on completion, max-iterations, an error streak of 3, or explicit stop
- [x] Make `Retry` re-arm the live worker rather than setting an event nothing awaits (pyocloop bug D1)
- [x] Make `Pause` show pending-vs-active clearly, and allow `abort`-then-pause for an immediate stop
- [x] Ensure `quit` always tears the subprocess down, including on exception paths (use `try/finally`, not just the quit action)

### Phase 5 — CLI and bootstrap (`cli.py`)

- [x] Implement `piloop run` with the option set in DESIGN §3; drop `-p/--port`; expose `--append-system-prompt`/`--skill` instead of pyocloop's PI-less `-a/--agent`
- [x] Resolve `--prompt` and `--plan` to absolute paths immediately (keep pyocloop's path fix)
- [x] Validate that `pi` is on PATH and report its version at startup; warn if outside the tested range (developed against 0.82.1)
- [x] Implement `piloop bootstrap` writing `PLAN.md` + `.loop-prompt.md` templates, with `--force`
- [x] Update the loop-prompt template for PI (same task-selection contract; drop OpenCode-specific wording)

### Phase 6 — Verification

- [x] Add an end-to-end test against the fake `pi`: a 3-task plan runs to `<plan-complete>` without network access
- [x] Add a regression test asserting no wait is unbounded: with a fake `pi` that accepts a prompt and then goes permanently silent, the run must terminate via the watchdog
- [x] Add a regression test for the extension-dialog hang: a fake `pi` emitting a `select` with no `timeout` must not stall the loop
- [x] Run a real end-to-end trial on a small plan with `zai/glm-5.2`, with `--log` enabled, and confirm clean completion
- [x] Write `README.md`: what it is, install, usage, PI setup, differences from pyocloop, and a short "why the rewrite is more reliable" section referencing `ANALYSIS.md`
- [ ] [MANUAL] Take a screenshot for the README
- [x] Create the GitHub repository and push

### Phase 7 — Upstream the fixes to pyocloop (optional)

- [ ] Port back the critical fixes to `../pyocloop`: drain the server stdout pipe (H2), bound the idle wait with a real deadline (H1), reject empty-session-ID idle events (D2), and stop the loop on an error streak (D3)
