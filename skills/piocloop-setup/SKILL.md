---
name: piocloop-setup
description: Generate piocloop loop files (a PLAN.md task list and a .loop-prompt.md instruction file) so the PI coding agent can iteratively work through a backlog one task per session. Use when the user wants to run the same job for each item in a list or batch, asks to loop over a backlog with pi, or mentions piocloop, piloop, PLAN.md, loop-prompt, or orchestrating a repetitive task across many items. Also configure optional timed execution with weekly run/pause windows; use for time slots, scheduling, weekday/weekend hours, or overnight runs.
license: MIT
compatibility: Requires piocloop installed (piloop on PATH) and the PI coding agent (pi on PATH, developed against 0.82.x). Python 3.11+. Timed execution requires piocloop 0.1.4+.
metadata:
  author: rbarzic
  version: "1.1"
  project: piocloop
---

# Set up a piocloop run

piocloop (`piloop`) is a loop harness that drives the
[PI coding agent](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
(`pi`) through a backlog **one task per session**: it reads a `PLAN.md`, sends
the next pending task to `pi`, waits for the agent to settle, and repeats until
the plan is done. piocloop does **not** call any LLM itself — it delegates all AI
work to the `pi` binary.

Your job when this skill is activated: turn the user's request into the **two
files** piocloop needs, then tell them how to launch the loop.

## The two files you must produce

| File (default name) | Role                                                              |
| ------------------- | ----------------------------------------------------------------- |
| `PLAN.md`           | The backlog — a markdown task list using checkbox syntax.         |
| `.loop-prompt.md`   | Instructions `pi` follows on **every** iteration, for one task.   |

Both must live in the directory the user specifies (the loop's working
directory). Note `.loop-prompt.md` is a **dotfile** — `piloop run` defaults its
`--prompt` to this exact hidden name, and `piloop bootstrap` creates it so.

## The request pattern this skill handles

A typical request looks like:

> "For each **XXXXX** items do **YYYYYY** and update **ZZZZZZ.md** file
> accordingly. Put the files in the directory **DDDDDDDDDD**."

Map it like this:

- **XXXXX** (the item list) → one `- [ ]` task line per item in `PLAN.md`
- **YYYYYY** (the action) → the `Execute:` body in `.loop-prompt.md`
- **ZZZZZZ.md** (the output) → a step in `.loop-prompt.md` that updates this
  file; also referenced in the plan's Overview
- **DDDDDDDDDD** (the location) → where both files are written

## Procedure

1. **Create the target directory** (DDDDDDDDDD) if it does not exist.

2. **Write `PLAN.md`** — start from `assets/PLAN.template.md` and:
   - Replace the title and Overview with the real goal.
   - Add **one** `- [ ]` task line per item from XXXXX. Each task must be
     self-contained: every iteration runs in a **fresh `pi` session** with no
     memory of previous ones, so a task that says "now do the same for the next
     one" will fail.
   - Group tasks into `### Phase N: ...` sections of ~10 tasks each. Phases are
     worked **strictly in order** — finish phase N before N+1.
   - If a task needs a human, mark it `- [MANUAL] description` (the loop skips it).
   - Use stable numbering (`**1**`, `**2**`, …) so tasks are easy to reference.

3. **Write `.loop-prompt.md`** — start from `assets/loop-prompt.template.md` and:
   - **No YAML frontmatter.** piocloop sends this file to `pi` verbatim as the
     prompt text; any frontmatter would be read by the model as literal content.
     (This differs from pyocloop/OpenCode, which required frontmatter.)
   - **Always** use the literal placeholder `{{PLAN_FILE}}` wherever the plan is
     referenced — piocloop injects the absolute path at runtime. Never hardcode
     a path.
   - In `Execute:`, put the YYYYYY action the user asked for.
   - In `After completion:`, add a step that updates the ZZZZZZ.md output file.

4. **Tell the user how to run it** (see "Launch" below). Do **not** run the loop
   yourself unless explicitly asked — `piloop run` opens an interactive TUI.
   If time slots are requested, read
   `references/timed-execution/scheduling.md` and add the requested windows and
   timezone to the launcher. Leave scheduling absent when not requested.

## Key rules (these break the loop if violated)

- **`{{PLAN_FILE}}` is mandatory.** The loop prompt must read and update the plan
  via this placeholder; piocloop replaces it with the absolute path. It is the
  only placeholder piocloop substitutes.
- **Mark `[x]` after EACH task**, not all at once at the end. The loop picks the
  *first* pending (`- [ ]`) line. In piocloop this is enforced: if the number of
  completed tasks does not increase for 3 consecutive iterations, the run halts
  in a `STALLED` state (see Gotchas).
- **`<plan-complete>` ends the run.** When every non-`[MANUAL]` task is `[x]` or
  `[BLOCKED: reason]`, the prompt must append to `PLAN.md`, **starting at the
  beginning of a line**: `<plan-complete>short summary</plan-complete>`.
- **Exact checkbox spellings** (parsed by piocloop, see `references/PLAN-FORMAT.md`):
  `- [ ]` pending · `- [x]` or `- [X]` done · `- [MANUAL]` skip ·
  `- [BLOCKED: reason]` cannot do.
- **One task per session** is the reliable default. Do not instruct `pi` to batch
  tasks unless the user explicitly asks.

## Gotchas

- **Stall detection counts `[x]`, not `[BLOCKED]`.** Progress is measured by the
  *completed* count only. If the agent marks three tasks `[BLOCKED: …]` in a row
  without completing any, the loop stops as `STALLED` even though it was working
  as intended. If a run is expected to block often, tell the user to raise or
  disable it: `--max-stalls 10` or `--max-stalls 0`.
- **`<plan-complete>` must start at column 0.** An indented tag is not matched
  and the loop will keep running. Content may span multiple lines, but the
  opening tag must begin the line. The *last* such block in the file wins.
- **Blocked tasks hold the percentage below 100%.** `[MANUAL]` items are excluded
  from the progress denominator; `[BLOCKED]` ones are not. That is cosmetic —
  completion is decided by the `<plan-complete>` tag, not the percentage.
- `.loop-prompt.md` starts with a **dot** (hidden file). Don't name it
  `loop-prompt.md` unless you also pass `--prompt` explicitly.
- `piloop run` **validates** that both files exist and refuses to start without
  them (use `--debug` to skip, but normally just create them).
- Tasks are matched in **file order**, first pending wins — so the order in the
  file IS the execution order.
- **Blocking dialogs from `pi` extensions are auto-answered** (default
  `--dialog-policy cancel`) so an unattended run cannot hang waiting for input.
  If the user's workflow depends on approving prompts, warn them and suggest
  `--dialog-policy allow`.
- If you'd rather start from piocloop's own starter files, run
  `piloop bootstrap <dir>` (creates both), then customize. Use `-f` to overwrite.

## Launch

After writing both files, give the user the command. Run from inside the
directory (defaults apply):

```bash
cd DDDDDDDDD
piloop run --model <provider/model> --run
```

…or run from outside by passing explicit paths:

```bash
piloop run \
  --plan   DDDDDDDDD/PLAN.md \
  --prompt DDDDDDDDD/.loop-prompt.md \
  --model  <provider/model> --run
```

- `<provider/model>`: list with `pi --list-models`. Examples: `zai/glm-5.2`,
  `anthropic/claude-sonnet-5`. Let the user pick if not specified; omitting
  `--model` uses pi's configured default.
- `piloop doctor` checks that `pi` is installed and reports its version.
- `--run` requests automatic start, subject to any configured windows; omit it
  to start manually by pressing `S` (also subject to scheduling).
- Useful guards: `--max-iterations N` (default 100), `--iteration-timeout S`
  (default 1800), `--max-stalls N` (default 3), `--log run.log`.
- TUI keys: `S` start · `Space` pause/resume · `A` abort current iteration ·
  `R` retry/continue · `Q` quit.

## Timed execution (piocloop 0.1.4+)

Only add time restrictions when the user requests them. Read
[the scheduling reference](references/timed-execution/scheduling.md) before
configuring time slots; clarify run versus pause periods, weekday/weekend hours,
and the intended IANA timezone rather than guessing.

- `--run-window EXPR` permits new jobs inside any supplied run window (repeatable).
- `--pause-window EXPR` blocks new jobs inside any supplied pause window
  (repeatable); **pause takes precedence**.
- `--schedule-timezone ZONE` applies to all windows; its default is **UTC**, not
  machine-local time. Quote each OSM weekly expression as one shell argument.
- Put these flags in the launch command or launcher script, **not** in
  `PLAN.md` or `.loop-prompt.md`. Never ask the task agent to sleep, interrupt
  work, or enforce the schedule itself.
- A job is one iteration/Pi session. Closing a window lets the active job
  finish normally; only new jobs wait. Watchdogs and explicit aborts remain active.
- Manual pauses and error/stall/max-iteration holds remain independent.
  `Space` and `R` cannot override a closed schedule.

For example, append these arguments to the selected launcher to permit Friday
night with a Saturday maintenance exclusion (adapt times and zone to the request):

```bash
--run-window 'Fr 22:00-06:00' \
--pause-window 'Sa 02:00-03:00' \
--schedule-timezone Europe/Paris
```

`Fr` names the starting day. Midnight introduces no gap or pause/resume
transition. For weekday/weekend, equivalent split-window, and adjacent-pause
configurations, load [the launch examples](references/timed-execution/examples.md).

## References (load on demand)

- Read `references/PLAN-FORMAT.md` for the **exact checkbox / blocked / manual
  syntax and parsing rules**, phasing, and the completion tag.
- Read `references/PROMPT-AUTHORING.md` when the user wants a **custom or
  advanced loop prompt** (multi-file output, research capture, batching,
  manual steps).

- Read `references/timed-execution/scheduling.md` whenever the user asks for time
  slots, off-hours execution, run/pause windows, timezone handling, or overnight
  schedules. It links to executable-form examples and the supported syntax.

These references are optional for an unscheduled standard "for each item do X"
request; the templates in `assets/` plus the rules above are usually enough.
