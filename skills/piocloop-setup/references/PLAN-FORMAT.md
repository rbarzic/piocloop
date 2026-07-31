# PLAN.md format reference

Detailed rules for `PLAN.md`, derived from piocloop's parser
(`src/piocloop/plan_parser.py`). Load this when you need exact syntax.

## Task line syntax

Every task is a single markdown list item starting with `- [`. The parser reads
the **checkbox** (text between `[` and the first `]`) and the **description**
(text after `]`):

```
- [ ]               pending      ← the agent picks the FIRST of these
- [x]   or - [X]    completed
- [MANUAL]          manual       ← skipped by the loop (human task)
- [BLOCKED: reason] blocked      ← cannot complete; reason recorded
```

Equivalent inline spellings are also accepted when the checkbox is empty:

```
- [] [MANUAL] description
- [ ] [BLOCKED: reason] description
```

Prefer the primary forms (`- [ ]`, `- [MANUAL]`, `- [BLOCKED: reason]`).

## Rules

- **Match is line-based.** A line only counts as a task if, when stripped, it
  starts with `- [`. Indented sub-bullets (`  - [ ]`) are still parsed as tasks,
  so keep tasks at the same indent level to avoid surprises.
- **Order is execution order.** The loop always selects the *first* pending
  (`- [ ]`) line in the file. Whatever appears first is what runs next.
- **`[x]` must be set by the prompt, immediately after a task finishes.** The
  parser recomputes progress from the checkboxes each iteration, and piocloop
  watches that number — see "Stall detection" below.
- **`[BLOCKED: reason]`** lets the agent record a task it could not do and move
  on. Blocked tasks are "settled" for completion purposes (the prompt may then
  append `<plan-complete>`), but they are **not** counted as completed.

## Stall detection (piocloop-specific)

piocloop compares the **completed** count after every iteration. If it does not
increase for `--max-stalls` consecutive iterations (default 3), the run stops in
a `STALLED` state rather than looping forever on a task the agent cannot finish.

Consequences to keep in mind when authoring a plan or prompt:

- A prompt that forgets to mark `[x]` halts the run after 3 iterations.
- Marking tasks `[BLOCKED: …]` does **not** count as progress. A run that
  legitimately blocks several tasks in a row will trip the detector; raise the
  budget with `--max-stalls 10`, or disable it with `--max-stalls 0`.
- The user can press `R` in the TUI to continue past a stall.

## Phasing

Group tasks under `### Phase N: <name>` headings. The recommended loop prompt
instructs the agent to:

- finish all tasks in phase N before touching phase N+1,
- pick the first pending task in the earliest incomplete phase.

Because execution order is file order, simply listing phases sequentially is
enough to enforce ordering. Keep phases small (≈10 tasks) for reliable progress
tracking in the TUI.

## Completion

The run ends when the loop prompt appends this to the plan:

```
<plan-complete>Short summary of work done and any remaining MANUAL tasks</plan-complete>
```

Parsing rules, exactly:

- The opening tag **must start at the beginning of a line** (column 0). An
  indented tag is ignored and the loop keeps going.
- The summary **may span multiple lines**; the closing tag ends it.
- If several blocks exist, the **last** one wins (so a resumed plan that
  accumulates markers behaves sensibly).

## Progress math (for context)

```
total     = all task lines
pending   = total - completed - manual - blocked
percent   = round(completed / (total - manual) * 100)
```

`[MANUAL]` items never count against completion, so a plan of only `[MANUAL]`
tasks reads as 100%. `[BLOCKED]` items **do** stay in the denominator, so a plan
that ends with blocked tasks finishes below 100% — that is expected, and does not
prevent the run from completing via `<plan-complete>`.
