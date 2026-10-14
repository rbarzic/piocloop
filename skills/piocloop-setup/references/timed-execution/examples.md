---
type: Playbook
title: piocloop time-slot launch examples
description: Launch weekday/weekend, maintenance, and overnight schedules using independent quoted window options.
resource: https://github.com/rbarzic/piocloop/tree/v0.1.4
tags: [piocloop, scheduling, launchers]
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
---

# piocloop time-slot launch examples

Apply the [scheduling rules](scheduling.md) before choosing an expression.
These are examples, **not default restrictions**. Confirm the user's requested
hours and timezone. Replace `<provider/model>` using the parent skill's model
policy; preserve existing `--plan`, `--prompt`, `--log`, and safety guards when
adding these flags to a launcher. Run from the directory containing `PLAN.md`
and `.loop-prompt.md`, or pass their paths explicitly. Do not launch the loop
unless asked.[^cli]

## Different weekday, Saturday, and Sunday times

```bash
piloop run --model '<provider/model>' --run \
  --run-window 'Mo-Fr 18:00-23:00; Sa 09:00-18:00; Su 12:00-16:00' \
  --schedule-timezone Europe/Paris
```

New jobs can start Monday-Friday 18:00-23:00, Saturday 09:00-18:00, and Sunday
12:00-16:00, evaluated in Europe/Paris. Outside those periods, the loop waits.

## Pause during business hours and daily maintenance

```bash
piloop run --model '<provider/model>' --run \
  --pause-window 'Mo-Fr 09:00-17:00' \
  --pause-window '02:00-03:00' \
  --schedule-timezone Europe/Paris
```

There is no run-window restriction. Either pause expression blocks a new job;
current jobs still finish normally.

## Run overnight with a maintenance exclusion

```bash
piloop run --model '<provider/model>' --run \
  --run-window '22:00-06:00' \
  --pause-window '02:00-03:00' \
  --schedule-timezone Europe/Paris
```

Every day's overnight window is permitted except 02:00-03:00. Pause wins over
run permission. A job started before 02:00 can finish after 02:00.

## Direct and split midnight windows are equivalent

Direct Friday night:

```bash
piloop run --model '<provider/model>' --run \
  --run-window 'Fr 22:00-06:00' \
  --schedule-timezone Europe/Paris
```

Equivalent independent segments:

```bash
piloop run --model '<provider/model>' --run \
  --run-window 'Fr 22:00-24:00' \
  --run-window 'Sa 00:00-06:00' \
  --schedule-timezone Europe/Paris
```

Friday 22:00 through Saturday 06:00 is continuous in both forms. At Saturday
00:00, one segment ends as the other starts: **no midnight pause/resume, abort,
or additional launch caused by crossing the boundary**. Saturday 06:00 is
excluded. If Saturday daytime is independently permitted too, add
`--run-window 'Sa 09:00-18:00'` as another option rather than joining rules with a
semicolon that could override overnight spillover.[^evaluator]

## Adjacent scheduled pauses remain blocked across midnight

```bash
piloop run --model '<provider/model>' --run \
  --pause-window 'Fr 23:00-24:00' \
  --pause-window 'Sa 00:00-01:00' \
  --schedule-timezone Europe/Paris
```

This blocks admission continuously from Friday 23:00 to Saturday 01:00. Midnight
does not open a launch opportunity; a job already running at 23:00 may finish.

[^cli]: Release 0.1.4 CLI options; model and launcher choices follow the parent skill.
[^evaluator]: Release 0.1.4 unions independent expressions before evaluating permission.
