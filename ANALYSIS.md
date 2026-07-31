# pyocloop — code analysis

Analysis of `../pyocloop` @ `57d000d` (v0.3.1), focused on why the loop sometimes
hangs. Findings are marked **CONFIRMED** (verified against a live `opencode`
1.18.7 server or by unambiguous code reading) or **PLAUSIBLE**.

---

## The hang: three independent mechanisms, all reachable

The loop's blocking point is `tui.py:446`:

```python
while not self._idle_event.is_set():
    try:
        await asyncio.wait_for(self._idle_event.wait(), timeout=IDLE_POLL_INTERVAL)
    except asyncio.TimeoutError:
        session = await self._client.get_session(self._current_session_id)
        if session.get("idle", False):      # <-- never true
            self._idle_event.set()
```

This `while` has **no exit other than `_idle_event` being set**. There is no
iteration deadline, no max-retry, no bail-out. So any failure to observe the
idle signal is an unrecoverable hang, not a slow iteration. Three separate
things can cause that.

### H1 — The polling fallback is a no-op **CONFIRMED**

`git log` shows commit `02e9008 fix(tui): add session-idle polling fallback`,
added precisely to rescue a missed `session.idle`. It cannot work: the OpenCode
session object has **no `idle` field**. Live response from `POST /session`:

```json
{"id":"ses_055ec9c8effeOIYm5v2tGw2MEo","slug":"sunny-forest","projectID":"global",
 "directory":"/tmp","path":"tmp","cost":0,"tokens":{...},"title":"New session - ...",
 "version":"1.18.7","time":{"created":1785264825201,"updated":1785264825201}}
```

`session.get("idle", False)` is therefore **always `False`**. The fallback polls
every 30s forever and never fires. The intended safety net does not exist.

### H2 — The OpenCode server can deadlock on its own stdout **CONFIRMED (mechanism)**

`opencode_server.py` spawns the server with `stdout=PIPE, stderr=STDOUT`, then
reads the pipe *only* until the "listening" banner:

```python
async for raw in self._proc.stdout:
    if "opencode server listening" in line:
        return m.group(1)          # <-- loop exits; nothing ever reads again
```

After that, **nothing drains the pipe for the rest of the run**. asyncio buffers
into a `StreamReader`, but its flow control calls `pause_reading()` at the 64 KiB
high-water mark; the OS pipe (another ~64 KiB) then fills, and the server
**blocks in `write()` and stops serving**. The client sees no further SSE events
→ H1 → permanent hang.

This matches the reported symptom precisely: works fine, then hangs
*sometimes*, after an unpredictable amount of work. It is output-volume
dependent, so a chatty local-LLM provider (extra warnings, retries, token
diagnostics on stderr — which is merged into the same pipe) reaches the
threshold much faster than a quiet cloud provider. **This is the most likely
cause of the hangs you observed, and it is a pyocloop bug, not an LLM problem.**

### H3 — SSE reconnect drops events permanently **CONFIRMED**

`subscribe_events` reconnects with backoff up to 30 s, but OpenCode's `/event`
stream has no replay and the client sends no `Last-Event-ID`. **Every event
emitted during the gap is lost forever.** If the lost event is `session.idle`,
the iteration hangs (H1 again). A clean disconnect sets `backoff = 5.0`, so
there is always at least a 5 s blind window per reconnect.

### Why the existing escape hatch doesn't help

`_reload_plan` (line 322) can set `_idle_event`, but only when
`is_plan_complete()` — i.e. only on the very last iteration. A stall on
iteration 3 of 20 is never rescued.

---

## Other defects

### D1 — "Retry" is dead **CONFIRMED**

Every error path in `_worker_loop` does `post_message(_LoopError(...)); return` —
the worker exits. `action_retry` only repaints the header to `READY`, and
`action_start_loop` then sets `_start_event`, which **no one is awaiting**. After
any loop error, `R` then `S` leaves the app looking ready but permanently dead.
Restarting the process is the only recovery.

### D2 — A stale idle event ends the wrong iteration **CONFIRMED**

`on__session_idle` accepts the event when the session ID is *empty*:

```python
if not msg.session_id or msg.session_id == self._current_session_id:
```

`_idle_event.clear()` happens at line 420, *before* the prompt is sent. A late
`session.idle` from iteration N-1 arriving in that window terminates iteration N
instantly → the loop spins, creating sessions that do no work. The server is
also shared and unauthenticated on a fixed port, so another client's events can
land here too.

### D3 — Errors don't stop the loop **CONFIRMED**

`on__session_error` sets `_state = STATE_ERROR` *and* sets `_idle_event`, so the
loop immediately starts the next iteration while the header reads `ERROR`. With
a persistently failing provider this becomes an unbounded hot loop hammering the
LLM. There is no error-streak circuit breaker.

### D4 — No progress-stall detection **CONFIRMED**

If the model does not tick a checkbox (very common with smaller local models),
the loop re-runs the identical task forever. Nothing compares plan progress
across iterations. Externally this is indistinguishable from a hang.

### D5 — Fixed port 4096 **CONFIRMED**

No fallback and no port-in-use detection; a second instance or a stale server
kills startup. Should use port 0 / auto-select.

### D6 — Pause is confusing **CONFIRMED (UX)**

`Space` during `RUNNING` only sets a flag checked *after* the current iteration
completes. `PAUSING` can persist for many minutes with no indication that the
pause is pending rather than stuck.

### D7 — Minor

- `on__session_created` resets `_total_tokens`, so the `tok:` field is
  per-iteration despite being named total, and it fires for *any* session
  created on the shared server.
- `parse_model_string("foo")` yields `providerID: ""` — likely rejected upstream.
- `_log_fh` is only closed in `_do_quit`; an exception path leaks it.
- Dead imports (`os`, `sys`, `signal` partially) in `opencode_client.py` /
  `opencode_server.py`.
- Blocked tasks count against `percent_complete` forever, so the bar never
  reaches 100% on a plan with blocked items.

---

## Verdict

Your instinct that the local LLM was to blame is understandable, but the
evidence points at pyocloop. **H2 (undrained stdout pipe) and H1 (a fallback
that can never fire) together produce exactly "it works, then it silently stops
forever"**, and the loop has no deadline that would ever break out. A stuck
local LLM would have produced the same visible symptom *only* because pyocloop
has no timeout to distinguish the two — which is itself the deeper flaw.

**Design lesson carried into piocloop: every wait gets a deadline, every pipe
gets a drainer, and lack of progress is a first-class detected state.**
