---
type: Runbook
title: Configure piocloop time slots
description: Configure weekly run and pause windows without interrupting active jobs or overriding manual and recovery holds.
resource: https://github.com/rbarzic/piocloop/tree/v0.1.4
tags: [piocloop, scheduling, timezones]
supported_release: "0.1.4"
status: stable
generated:
  by: openai-codex/gpt-6.1-sol
  at: 2026-10-14T08:11:53Z
sources:
  - id: cli
    resource: https://github.com/rbarzic/piocloop/blob/v0.1.4/src/piocloop/cli.py
  - id: evaluator
    resource: https://github.com/rbarzic/piocloop/blob/v0.1.4/src/piocloop/schedule.py
  - id: engine
    resource: https://github.com/rbarzic/piocloop/blob/v0.1.4/src/piocloop/loop.py
  - id: readme
    resource: https://github.com/rbarzic/piocloop/blob/v0.1.4/README.md
---

# Configure piocloop time slots

Time slots control **admission of new jobs**, not task execution inside a job.
A job is one loop iteration/Pi session. When permission closes, the current job
finishes normally; the next job waits until permission returns. Scheduling never
aborts an active job, but existing watchdog deadlines and explicit abort controls
still apply.[^engine]

## Setup procedure

1. Check that the target installation supports scheduling: piocloop **0.1.4+**.
   Inspect `piloop run --help` for all three options below; do not assume an old
   installation supports them. Do not install or upgrade software without consent.
2. If the user requests time slots, establish which periods permit running and
   which require pausing, the weekday/weekend times, and the intended timezone.
   Clarify ambiguous requests before writing flags. Do not assume machine-local
   time; the default is UTC.
3. Add quoted expressions to the launch command or existing launcher script.
   Preserve its chosen model, paths, logs, and safety guards. If the user has not
   requested scheduling, leave window options absent to retain existing behavior.
4. Keep scheduling **out of `PLAN.md` and `.loop-prompt.md`**. Do not add agent
   instructions to sleep, watch the clock, or interrupt work at a boundary. The
   loop engine enforces admission and resumes automatically.
5. Use the [launch examples](examples.md) and explain the control behavior below.
   Generating the launcher does not authorize running the interactive loop.

## Options and composition

| Option | Meaning | Default |
| --- | --- | --- |
| `--run-window EXPR` | Allow new jobs inside any supplied run expression; repeatable | No run expressions: unrestricted |
| `--pause-window EXPR` | Block new jobs inside any supplied pause expression; repeatable | No pause expressions: no scheduled block |
| `--schedule-timezone ZONE` | One IANA timezone for all expressions, e.g. `Europe/Paris` | `UTC`, not machine-local time |

New jobs require run permission **and** no matching pause window. Pause takes
precedence. Repeated options are independent union members. Rules separated by
semicolons **within one expression** retain OSM rule-override semantics, not an
unconditional union. Prefer separate options for independent overnight/daytime
periods. A matching pause expression means "pause"; do not append `off`.[^evaluator]

## Supported syntax and boundaries

Expressions use a restricted weekly subset of OSM `opening_hours`:

- Weekdays: `Mo Tu We Th Fr Sa Su`; lists/ranges such as `Mo,We,Fr` and `Mo-Fr`.
- Explicit `HH:MM-HH:MM` ranges, comma-separated clock ranges, day-specific
  semicolon rules, direct overnight ranges, and `24/7`.
- Starts are inclusive; ends are exclusive: `[start, end)`.
- `24:00` is valid only as an endpoint and means end-of-day midnight.
- A weekday on an overnight range names the **starting day**: `Fr 22:00-06:00`
  includes Saturday 00:00-06:00, not Friday morning.
- Adjacent independent ranges are continuous. Direct overnight and equivalent
  split expressions cause **no midnight gap, pause/resume transition, abort, or
  extra launch caused by the boundary**. Normal completion may still launch the
  next job if permission remains open.
- Equal endpoints are rejected; use `00:00-24:00` for a full day.
- Expressions are limited to 4096 characters. Holidays/date selectors, solar
  times, comments/modifiers (including `off`), and uncertain/open-ended ranges
  are unsupported. Do not advertise the full OSM language.[^evaluator]

## Waiting, controls, and timezone behavior

- `--run` or `S` requests starting the loop; it does not override scheduling.
  Starting inside a blocked period waits before the first job. Without `--run`
  or `S`, opening a permitted window does not start a ready loop by itself.
- During an active job, a block shows `PAUSING`; after it settles, `PAUSED`.
  The TUI/logs show the timezone, blocking reason, and next combined permitted
  time when known, not merely the end of an individual pause expression.
- `Space` toggles a **separate manual hold**. Opening the schedule never clears
  it. `R` retries error/stall/max-iteration holds, but cannot bypass the schedule
  or clear a simultaneous manual hold.
- Scheduled waiting does not count as a job or reset error/stall counters or
  execution-duration averages. Completion is reported without waiting for the
  next permitted window; `Q` ends scheduled waiting promptly.
- If permission closes and reopens before the job finishes, the next job can
  start normally. There is no stale scheduled-pause latch.
- Evaluate real instants in the selected zone's local wall time. Both occurrences
  of a repeated DST hour follow membership; nonexistent spring-forward times
  create no execution opportunities. After clock changes or machine suspension,
  reevaluate current permission instead of replaying missed triggers.[^engine]

This reference describes the tagged 0.1.4 implementation. Recheck it against
runtime code when scheduling semantics change; the generation date is not human
review. See [examples](examples.md) for weekday/weekend, maintenance, and midnight
configurations.

[^cli]: Release 0.1.4 CLI option definitions and validation.
[^evaluator]: Release 0.1.4 schedule validation and combined membership evaluation.
[^engine]: Release 0.1.4 admission gates, waiting, and independent controls.
[^readme]: Release 0.1.4 user-facing timed-execution documentation.
