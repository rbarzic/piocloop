# Loop prompt authoring guide

Load this when the user wants a custom or advanced `.loop-prompt.md` beyond the
standard "do one action, update one file" pattern.

## Anatomy

The file is **plain markdown with no frontmatter**:

```
<body — instructions pi follows each iteration>
```

- piocloop reads this file and sends it to `pi` **verbatim** as the prompt text.
  Anything you put in it, including a YAML header, is read by the model as
  content. Do not add frontmatter. *(pyocloop required a
  `description: Execute loop` header because OpenCode's prompt format demanded
  it; piocloop does not.)*
- The body is sent once per session, and each iteration is a **fresh session**
  with no memory of previous iterations.

## The one placeholder you must use

`{{PLAN_FILE}}` — piocloop rewrites this token to the **absolute path** of the
plan at runtime. Use it everywhere the plan is read or written. Never hardcode a
path, and do not invent other `{{...}}` placeholders (only `{{PLAN_FILE}}` is
substituted).

## Sections that work well

```
1. Before starting   → Read {{PLAN_FILE}} fully
2. Task selection    → first pending task, in-order phases, skip [MANUAL]/[BLOCKED]
3. Execute           → the actual job for this task
4. After completion  → mark [x], write outputs, record blockers
5. Completion check  → append <plan-complete> when nothing is pending
```

## Customization patterns

- **Output to a specific file:** add an explicit step in `After completion`:
  `Append the result for this item to <path/to/ZZZZZZ.md>`. Create the file on
  the first task; append on subsequent ones.
- **Multiple output files:** list each file and what should go in it.
- **Research / external knowledge:** instruct the agent to capture non-obvious
  findings into a `docs/<topic>.md` file and reference them from `AGENTS.md`, so
  later sessions reuse them instead of re-discovering. (`pi` reads `AGENTS.md`
  and `CLAUDE.md` automatically unless `--no-context-files` is set.)
- **Batching within a phase:** by default, one task per session. Only allow
  batching if the user explicitly asks, and only for tasks in the same phase and
  same file that are logically coupled.
- **Human-gated steps:** mark them `- [MANUAL] ...` in the plan and tell the
  prompt to skip `[MANUAL]` items.
- **Blocking gracefully:** if a task can't be done (permissions, external
  service), the prompt should write `- [BLOCKED: reason]` on that line and move
  on. Note this does not count as progress for stall detection — see
  `PLAN-FORMAT.md`.

## Marking done & finishing

Always include both:

```
After completion:
1. Update {{PLAN_FILE}} marking the finished item with [x]   # per task

Completion check:
- If all non-[MANUAL] tasks are [x] or [BLOCKED]:
  - Append `<plan-complete>SUMMARY</plan-complete>` to {{PLAN_FILE}},
    starting at the beginning of a line                      # once, at the end
  - Stop
```

The per-task `[x]` is how progress advances; the final `<plan-complete>` is how
the loop terminates. Missing the first halts the run at `STALLED` after 3
iterations; missing the second runs until `--max-iterations`.

## Tips

- Keep the prompt focused on **one task's** lifecycle — it runs once per session.
- Say "Stop" rather than "Exit the session": piocloop ends the iteration when the
  agent settles, and starts a new session itself.
- Be concrete about file paths, but route the *plan* through `{{PLAN_FILE}}`.
- Prefer telling the agent *what* a good result looks like over micromanaging
  *how*.
- If iterations are long, remind the user of `--iteration-timeout` (default 1800
  seconds), after which piocloop aborts the iteration and moves on.
