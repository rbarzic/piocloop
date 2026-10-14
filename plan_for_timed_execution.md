# Plan for timed execution

## Status and objective

This document records the design, implementation steps, and verified acceptance criteria for timed execution. The feature is implemented on branch `timed-execution`; CLI options shown below are available.

### Implementation and verification

- Added a scheduling evaluator in `src/piocloop/schedule.py`, admission control in the loop engine, and CLI/TUI support.
- Verified `opening-hours-py` 2.1.4 wheel metadata for Linux, macOS, and Windows; source builds require Rust. Runtime tests were performed on Linux with Python 3.11 and 3.12.
- The parser handles overnight expressions, but its `next_change()` can skip the first occurrence of an ambiguous autumn DST time. The implementation therefore materializes weekly membership using the parser and calculates combined transitions by scanning actual UTC minutes, not by trusting parser transition predictions.
- Only explicit minute-resolution weekly rules are accepted. Each expression is materialized over 10,080 wall-clock minutes. Transition search is bounded to 16 days, covering a skipped weekly spring-forward window and the next week's opening; results are cached per UTC minute. Constant allowed/blocked schedules require no transition scan.
- Added evaluator, controlled-job lifecycle, CLI, and TUI regression tests. Both Python versions passed all 220 tests, including injected-clock simulations of DST, clock jumps/suspension, and midnight continuity.
- Wheel/source-distribution builds, dependency compatibility, compilation, whitespace checks, and focused Ruff import/error checks passed. No live LLM/API calls were needed.

Allow users to define recurring time slots during which piocloop may start jobs or must pause its loop. Entering a blocked period must not interrupt the current job: it finishes normally, and the next job waits until the combined schedule permits execution.

A **job** means one existing loop iteration/Pi session, not an individual tool call within that session. Existing watchdog deadlines and explicit abort controls remain active; scheduling itself must never abort a running job.

## 1. Recommended syntax

Use OpenStreetMap's `opening_hours` syntax for compact CLI expressions. It describes continuous time windows rather than trigger instants, and supports weekdays, multiple ranges, and overnight periods.

| Expression | Meaning |
|---|---|
| `09:00-17:00` | Every day, 09:00–17:00 |
| `Mo-Fr 09:00-17:00` | Weekday business hours |
| `Mo-Fr 09:00-12:00,13:00-17:00` | Two windows per weekday |
| `Fr 22:00-06:00` | Friday night through Saturday morning |
| `24/7` | Always |

Reference: [OSM opening_hours specification](https://wiki.openstreetmap.org/wiki/Key:opening_hours/specification).

The Python library [`opening-hours-py`](https://remi-dupre.github.io/opening-hours-rs/opening_hours.html) provides parsing, timezone-aware evaluation, and next-state-change calculation. Reuse an established parser rather than implementing a new full scheduling language.

### Alternatives considered

| Syntax | Assessment |
|---|---|
| [Prometheus Alertmanager time intervals](https://prometheus.io/docs/alerting/latest/configuration/#time_interval) | Excellent structured alternative: weekday lists, start/end times, and IANA timezones. More verbose, but preferable if avoiding a native-backed parser dependency. |
| Cron | Describes trigger instants, not continuous windows. Paired pause/resume triggers complicate startup, restarts, and recovery from missed events. |
| [systemd calendar expressions](https://www.freedesktop.org/software/systemd/man/latest/systemd.time.html#Calendar%20Events) | Well documented, but also describes points in time rather than an execution-permission window. |

Decision: use OSM syntax. The dependency and compatibility spike is complete; the DST transition-prediction adaptation is described above.

### Initial syntax scope

Support deterministic weekly schedules:

- Weekday selectors, lists, and ranges.
- Explicit clock-time ranges.
- Multiple time ranges per day.
- Overnight ranges.
- `24/7`.
- Day-specific rules such as the weekday/Saturday/Sunday example below.

Defer holidays, solar times, and uncertain or open-ended expressions. The supported subset is validated explicitly, with actionable errors for unsupported constructs. Comments/modifiers (including `off`) are rejected. Equal start/end times are rejected as ambiguous; use `00:00-24:00` for a full day. Expressions are limited to 4096 characters. Do not silently accept an expression whose meaning cannot be evaluated reliably.

## 2. CLI

| Option | Meaning |
|---|---|
| `--run-window EXPRESSION` | Permit new jobs only inside matching windows; repeatable. |
| `--pause-window EXPRESSION` | Block new jobs inside matching windows; repeatable. |
| `--schedule-timezone ZONE` | IANA timezone used for all schedule evaluation. Default: `UTC`. |

A matching `--pause-window` expression means “pause”; users do not need to append `off`.

No schedule options means unchanged behavior. Scheduling does not replace the existing Start action or `--run`: a ready loop still needs to be started before it can execute jobs.

Display the resolved timezone in the TUI and logs. Do not silently interpret a UTC schedule as machine-local time.

### Pause during business hours and maintenance

```bash
piloop run --run \
  --pause-window 'Mo-Fr 09:00-17:00' \
  --pause-window '02:00-03:00' \
  --schedule-timezone Europe/Paris
```

New jobs are blocked during weekday business hours and the daily maintenance period. An already-running job finishes normally.

### Run overnight, except during maintenance

```bash
piloop run --run \
  --run-window '22:00-06:00' \
  --pause-window '02:00-03:00' \
  --schedule-timezone Europe/Paris
```

New jobs may start overnight, except during 02:00–03:00. Outside the overnight window, the loop waits.

### Different windows for weekdays, Saturday, and Sunday

```bash
piloop run --run \
  --run-window 'Mo-Fr 18:00-23:00; Sa 09:00-18:00; Su 12:00-16:00' \
  --schedule-timezone Europe/Paris
```

| Day | New jobs may start |
|---|---|
| Monday–Friday | 18:00–23:00 |
| Saturday | 09:00–18:00 |
| Sunday | 12:00–16:00 |

Outside these windows, the loop waits. A job already running may finish after its window closes.

The same expression can be passed to `--pause-window` to block these periods instead.

## 3. Combined scheduling semantics

At any instant, permission to start a job is:

```text
may_start_job =
    (no run windows configured OR inside any run window)
    AND NOT inside any pause window
    AND NOT manually paused
    AND NOT held by an error/stall
    AND NOT stopping
```

Rules:

1. Repeated run-window options form a union.
2. Repeated pause-window options form a union.
3. Pause windows take precedence over run windows.
4. Each option's expression is evaluated independently, then its result is combined with the other options.
5. Inside an expression, preserve the supported OSM rule semantics; do not reinterpret semicolons as a universal union operator.
6. Starts are inclusive and ends are exclusive: `[start, end)`.
7. Waiting does not increment iteration, stall, or error counters, and does not contribute to iteration execution-time averages.
8. Schedule changes never automatically clear manual pause, error, stall, or max-iteration recovery holds.
9. Space/Retry must not bypass the schedule. They only affect the relevant manual or recovery hold.
10. Plan completion takes precedence over scheduled waiting.

**Core design rule: evaluate combined permission at the current instant. Never toggle permission separately when each window starts or ends.**

This avoids stale state after restarts, machine suspension, clock changes, and overlapping or adjacent windows.

## 4. Overnight windows and midnight safety

### Direct overnight notation

Splitting an overnight window is not required:

```bash
--run-window 'Fr 22:00-06:00'
```

This permits new jobs from **Friday 22:00 until Saturday 06:00**. The selected weekday is the day on which the window starts. The early-morning portion belongs to the following calendar day.

Midnight is inside this continuous window: **there must be no pause, resume transition, abort, or extra job launch simply because the date changes**. Normal job completion may still lead to another job starting if permission remains true.

Similarly:

```bash
--pause-window '22:00-06:00'
```

blocks new jobs every night. The loop remains blocked across midnight, while the current job is allowed to finish.

### Equivalent split notation

Users may also split the Friday overnight window into independently configured windows:

```bash
--run-window 'Fr 22:00-24:00' \
--run-window 'Sa 00:00-06:00'
```

These options must produce the same permission as `Fr 22:00-06:00`.

- `24:00` means midnight at the end of the selected day; it is an endpoint, not a clock hour used to construct a normal Python `time` value.
- At Saturday `00:00`, the Friday segment no longer matches, but the Saturday segment already matches.
- Because the combined result is a union, there is no gap or intermediate state change.
- Adjacent pause windows must behave identically: the loop remains blocked across their shared boundary.

Do not process “end first, then start” as two control actions. Compute the complete decision once for the timestamp.

### Combining overnight and daytime schedules

OSM rule overrides can be subtle, particularly when a later day's rule interacts with an overnight spillover. Use separate options for independent windows:

```bash
--run-window 'Fr 22:00-06:00' \
--run-window 'Sa 09:00-18:00'
```

Expected Saturday behavior:

| Time | New jobs |
|---|---|
| 00:00–06:00 | Allowed, continuing Friday's window |
| 06:00–09:00 | Blocked |
| 09:00–18:00 | Allowed |
| After 18:00 | Blocked, unless another run window matches |

Document this distinction clearly: repeated options are independent union members; rules inside one expression follow OSM semantics.

## 5. Job lifecycle and pause behavior

### Entering a blocked period while a job runs

- Continue the current iteration without scheduling-induced aborts.
- Show `PAUSING`, with a scheduled-pause reason.
- When the iteration finishes, reevaluate the schedule before starting another job.
- If still blocked, enter `PAUSED` and wait.
- If the blocked period has already ended, proceed without retaining a stale pause request.

### Starting while blocked

- With `--run`, or after the user presses Start, wait before the first job if the schedule blocks execution.
- Do not send a job prompt while blocked.
- Idle readiness checks during initial boot are not jobs; avoid unnecessary per-iteration subprocess launches while waiting.

### Leaving a blocked period

- Resume automatically only if all other admission conditions permit execution.
- Leaving one pause window does not resume the loop if another pause window still matches.
- Leaving a scheduled pause does not clear a manual pause.
- A schedule transition does not recover an error, stall, or max-iteration hold automatically.

### Completion, retries, and quitting

- If the plan is complete, finish immediately rather than waiting for the next permitted window.
- Every new iteration, including retry iterations, passes through the same admission gate.
- Existing in-job watchdog recovery remains in force; scheduling does not disable deadlines or change an active job into an unbounded wait.
- Stop/Quit must wake a schedule wait promptly.

## 6. Timezone and clock behavior

- Use timezone-aware timestamps and an explicit IANA timezone.
- Default timezone: `UTC`; examples explicitly select `Europe/Paris`.
- Window boundaries are local wall-clock times in the selected timezone.
- Keep elapsed job duration and watchdog deadlines on a monotonic clock.
- After sleep, resume, or wall-clock adjustment, evaluate the current instant again instead of replaying missed start/stop events.
- Use bounded waits even when the next scheduled transition is far away.

For daylight-saving transitions, define and test permission in terms of the actual instant converted to local wall time:

- Repeated local times are evaluated on both occurrences according to the same window membership rules.
- Nonexistent local times do not create fictitious execution opportunities; evaluate the local time that actually exists after the clock jump.
- Verify the parser's next-change calculation across both transitions. If its behavior does not match the admission contract, adapt transition calculation or reconsider the dependency.

The final admission check, not an earlier predicted wake time, decides whether a job may start.

## 7. Implementation steps

### Step 1: Validate the parser dependency

- [x] Verify compatibility with the project's Python 3.11+ requirement.
- [x] Verify available wheels for supported platforms and document any Rust build requirement.
- [x] Verify weekday selectors, multiple ranges, day-specific rules, and `24/7`.
- [x] Verify direct overnight ranges and `24:00` endpoints.
- [x] Verify timezone-aware evaluation and both daylight-saving transitions.
- [x] Verify next-change behavior for permanent states and schedules with no future permitted period.
- [x] Define validation for the supported initial syntax subset.
- [x] Establish bounded evaluation behavior for accepted expressions.

Do not claim support based only on successful parsing. Confirm evaluation and boundary behavior with concrete tests.

### Step 2: Add a standalone scheduling module

Create `src/piocloop/schedule.py` with:

- Parsed run/pause expressions and timezone.
- A pure evaluator accepting an explicit current timestamp.
- A decision containing permission, blocking reasons, and the next relevant transition when known.
- An injectable clock or equivalent deterministic test seam.
- Independent expression evaluation followed by union and precedence handling.

A change in an individual expression does not necessarily change combined permission. The reported resume time must account for all matching pause windows and run-window restrictions. If no future permission can be determined, show that explicitly rather than inventing a resume time or busy-spinning.

### Step 3: Integrate admission control into the engine

Modify `src/piocloop/loop.py`:

- [x] Extend `LoopConfig` with optional schedule configuration.
- [x] Gate every new iteration, including the first iteration and retries.
- [x] Check permission before starting the next iteration's subprocess lifecycle where practical.
- [x] Recheck immediately before prompt dispatch, because readiness awaits can cross a window boundary.
- [x] Define job admission at the final check before prompt dispatch; only an admitted job is counted as a running iteration.
- [x] Keep manual and scheduled pause reasons separate.
- [x] Monitor schedule changes during active jobs for display purposes without aborting them.
- [x] Implement interruptible, bounded schedule waits and reevaluate after every wakeup.
- [x] Check completion before scheduled waiting, including after the current iteration finishes.
- [x] Preserve recovery holds and counters during scheduled waiting.
- [x] Clean up any monitor tasks during shutdown.

The existing manual pause already waits for the current iteration to finish, but its state cannot be reused blindly for automatic scheduling. In particular, `_wait_for_resume()` currently clears pause state and resets recovery counters. Schedule waiting must not inherit those side effects.

Use an explicit control wakeup mechanism that does not lose concurrent stop/resume notifications. Wakeups request reevaluation; they are not permission to bypass the schedule.

### Step 4: Add CLI and TUI support

In `src/piocloop/cli.py`:

- [x] Add repeatable `--run-window` and `--pause-window` options.
- [x] Add `--schedule-timezone`.
- [x] Validate expressions and timezone before booting Pi.
- [x] Give clear errors for malformed expressions and unsupported initial-scope constructs.

In `src/piocloop/tui.py`:

- [x] Display schedule blocking reasons independently of manual and recovery holds.
- [x] Display the timezone and next effective permission time when known.
- [x] Ensure Space/Retry cannot bypass scheduled restrictions.
- [x] Log state transitions, not every schedule evaluation.

Example status text:

```text
PAUSING — scheduled window entered; finishing current iteration
PAUSED — scheduled pause until 17:00 Europe/Paris
PAUSED — outside run windows; next permitted start at 22:00 Europe/Paris
PAUSED — manual pause + scheduled pause
```

Preserve error/stall status when it remains the controlling hold. A schedule monitor must not overwrite it with an apparently resumable scheduled pause.

### Step 5: Tests and documentation

Add scheduling unit tests, engine integration tests, CLI validation tests, and TUI tests. Use a fake/injected clock rather than real multi-hour waits.

`README.md` documents the options, examples, timezone behavior, independent-option union rules, and the full midnight safety contract.

## 8. Acceptance criteria

### Expressions and boundaries

- [x] Exact start-inclusive/end-exclusive behavior.
- [x] Different weekday, Saturday, and Sunday schedules.
- [x] Multiple ranges per day.
- [x] Overlapping run windows and overlapping pause windows.
- [x] Pause precedence when run and pause windows overlap.
- [x] Direct overnight notation equals its independently configured split equivalent.
- [x] Crossing midnight inside a continuous window causes no pause/resume transition, abort, or extra launch caused by the boundary itself.
- [x] Adjacent pause windows never briefly permit a new job.
- [x] Adjacent run windows never briefly block one.
- [x] Correct overnight behavior across week, month, and year boundaries.

### Engine behavior

- [x] Startup inside a blocked period waits before the first job prompt.
- [x] Entering a blocked period does not abort the running job.
- [x] A job finishing while still blocked is followed by schedule waiting.
- [x] A job finishing after the blocked period ends is followed by normal execution, without a stale pause.
- [x] Completion is reported without waiting for another permitted window.
- [x] All new iterations and retries are gated.
- [x] A window closing during subprocess readiness prevents subsequent prompt dispatch.
- [x] Manual pause persists after a scheduled pause ends.
- [x] Space/Retry does not bypass the schedule.
- [x] Schedule transitions do not clear error, stall, or max-iteration holds.
- [x] Waiting does not increment counters or contaminate iteration timing statistics.
- [x] Stop/Quit interrupts waiting promptly.
- [x] Unconfigured scheduling leaves existing behavior unchanged.

### Clock and validation behavior

- [x] Both daylight-saving transitions follow the documented timezone contract.
- [x] Clock jumps and machine suspend/resume are handled by current-state reevaluation.
- [x] Permanent allowed/blocked schedules do not busy-spin or invent transition times.
- [x] Invalid timezone, malformed expressions, and unsupported constructs fail clearly before agent startup.
- [x] Tests confirm the parser's supported subset and platform compatibility.

## 9. Scope boundaries

This feature controls admission of new jobs; it does not promise that all activity stops at a window boundary.

Initial scope does not include:

- Killing or suspending an active job at a scheduled time.
- Automatically overriding manual pause or recovery holds.
- Holiday calendars, astronomical/solar windows, or uncertain/open-ended rules.
- Live schedule-file reload or a new general configuration-file format.
- A separate cron-style background trigger service.

Any later extension must preserve the union-based admission model and midnight safety guarantees.
